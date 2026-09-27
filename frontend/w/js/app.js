// Worker PWA: link phone -> PIN -> pick orders -> end shift.
//
// Offline rules this file keeps:
//  * Every scan is decided locally (state.classify) and written to the
//    IndexedDB outbox before anything touches the network.
//  * Orders are cached with their match index when opened or prefetched, so
//    picking works with no signal at all.
//  * The server's answer is authoritative; when it disagrees with what the
//    phone showed, the worker is told what to do about it physically.

import { ApiError, request } from "../../shared/api.js";
import { brandLockup, dialog, h, mount, toast, uuid4 } from "../../shared/dom.js";
import * as FX from "./feedback.js";
import { LANGUAGES, T, getLang, setLang } from "./i18n.js";
import { snapshot, takePhoto } from "./photo.js";
import { DEFAULTS as PREF_DEFAULTS, cleanScan, keyGapMs, loadPrefs, savePrefs, usesHardwareScanner } from "./prefs.js";
import { Scanner } from "./scanner.js";
import * as S from "./state.js";
import { requestPersistence, store } from "./store.js";
import { Sync } from "./sync.js";
import { reportErrors } from "../../shared/report-errors.js";

reportErrors("phone");

const LS_DEVICE = "ar.device";
const LS_SESSION = "ar.session";
const LS_LANG = "ar.lang";
const CAMERA_IDLE_MS = 20000;
const MATCH_OVERLAY_MS = 900;
const PREFETCH_LIMIT = 40;
const FLAG_REASONS = ["wrong_item_in_location", "out_of_stock", "damaged", "label_unreadable", "other"];
const SHORT_REASONS = ["out_of_stock", "not_found", "damaged", "other"];
const MAX_PHOTOS = 4;

const root = document.getElementById("app");
// The desktop pack station: same app, laid out for a laptop and a USB scanner.
const STATION = document.body.dataset.mode === "station";

const app = {
  device: readJson(LS_DEVICE), // {token, warehouseName, label}
  session: readJson(LS_SESSION), // {token, id, workerId, workerName, expiresAt}
  screen: null,
  order: null, // cached payload of the order being picked
  batch: null, // {id, number, orders: [payload + tote]} while batch picking
  packShots: {}, // order id -> box photos waiting to upload
  pending: [], // outbox events (all orders)
  history: [], // recent scans on this phone, for undo
  targetLineId: null,
  overlay: null,
  status: { online: navigator.onLine, pending: 0, syncing: false },
  locked: null,
  camera: null, // {scanner, idleTimer, tickTimer, video}
  sync: null,
  wedgeBuffer: "",
  wedgeAt: 0,
  wedgeTimer: null,
  prefs: loadPrefs(STATION ? { scanner: "wedge" } : {}),
  multiBox: {}, // order id -> true when the worker said it ships in several boxes
  shift: null, // open time-clock shift, if any
  timeClock: false,
};

function readJson(key) {
  try {
    return JSON.parse(localStorage.getItem(key) || "null");
  } catch {
    return null;
  }
}

function writeJson(key, value) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* private mode: the app still works for this page load */
  }
}

// ---------------------------------------------------------------------------
// API with the phone's credentials, and the global error cases
// ---------------------------------------------------------------------------

async function api(path, opts = {}) {
  try {
    return await request(path, {
      ...opts,
      deviceToken: app.device && app.device.token,
      token: opts.noSession ? undefined : app.session && app.session.token,
    });
  } catch (e) {
    if (e instanceof ApiError) {
      if (e.status === 401 && e.code === "device_unlinked") {
        unlink();
      } else if (e.status === 401 && e.code === "worker_session_expired") {
        endSessionLocally();
        toast(T("sessionExpired"), "warn");
        showPin();
      } else if (e.status === 402) {
        app.locked = e.message;
        showLocked();
      } else if (e.status === 403 && e.code === "notice_required") {
        showNotice(app.noticeVersion || "1");
      }
    }
    throw e;
  }
}

function unlink() {
  stopCamera();
  writeJson(LS_DEVICE, null);
  writeJson(LS_SESSION, null);
  app.device = null;
  app.session = null;
  if (app.sync) app.sync.stop();
  showUnlinked();
}

function endSessionLocally() {
  stopCamera();
  writeJson(LS_SESSION, null);
  app.session = null;
  app.order = null;
}

// ---------------------------------------------------------------------------
// Chrome shared by every screen
// ---------------------------------------------------------------------------

function statusChip() {
  const s = app.status;
  let cls = "chip-ok";
  let label = T("synced");
  if (!s.online) {
    cls = "chip-off";
    label = s.pending > 0 ? `${T("offline")} · ${T("queued", { n: s.pending })}` : T("offline");
  } else if (s.syncing && s.pending > 0) {
    cls = "chip-sync";
    label = T("syncing");
  } else if (s.pending > 0) {
    cls = "chip-sync";
    label = T("queued", { n: s.pending });
  }
  return h("span", { class: ["chip", cls], id: "sync-chip", role: "status" }, h("span", { class: "dot" }), label);
}

function refreshChip() {
  const old = document.getElementById("sync-chip");
  if (old) old.replaceWith(statusChip());
}

/** Shows the current language in its own words; tap to pick another. */
function langToggle() {
  const current = (LANGUAGES.find(([code]) => code === getLang()) || LANGUAGES[0])[1];
  return h("button", {
    class: "link-btn lang-btn",
    "aria-label": T("language"),
    onclick: () => dialog(T("language"), (close) => h("div", { class: "lang-list" },
      ...LANGUAGES.map(([code, name]) => h("button", {
        class: ["btn btn-lg", code === getLang() && "btn-primary"],
        lang: code === "zh" ? "zh-Hans" : code,
        onclick: () => close(code),
      }, name)))).then((code) => {
      if (!code) return;
      writeJson(LS_LANG, setLang(code));
      rerender();
    }),
  }, "🌐 ", current);
}

function topbar(...right) {
  return h("header", { class: "topbar" },
    h("div", { class: "topbar-left" },
      brandLockup(),
      app.device ? h("span", { class: "topbar-title" }, app.device.warehouseName) : null),
    h("div", { class: "topbar-right" }, statusChip(), ...right));
}

function rerender() {
  const screens = {
    link: showLink, pin: showPin, orders: showOrders, pick: showPick, complete: showComplete, locked: showLocked,
    batch: showPick, restock: showRestock, clock: showClockIn,
    notice: () => showNotice(app.noticeVersion || "1"),
  };
  (screens[app.screen] || showPin)();
}

function setScreen(name, ...children) {
  app.screen = name;
  mount(root, ...children);
  window.scrollTo(0, 0);
}

// ---------------------------------------------------------------------------
// Link this phone
// ---------------------------------------------------------------------------

function codeFromText(text) {
  const t = String(text).trim();
  try {
    const url = new URL(t);
    const code = url.searchParams.get("link");
    if (code) return code;
  } catch {
    /* not a URL */
  }
  return t;
}

function showLink(prefill = "") {
  let busy = false;
  const code = h("input", {
    class: "input input-xl", id: "code", autocomplete: "off", autocapitalize: "characters", spellcheck: "false",
    placeholder: T("linkCodePlaceholder"), value: prefill, maxlength: "20",
  });
  const label = h("input", { class: "input", id: "label", placeholder: T("linkLabelPlaceholder"), maxlength: "100" });
  const err = h("p", { class: "form-error", role: "alert" });
  const submit = async (e) => {
    if (e) e.preventDefault();
    if (busy || !code.value.trim()) return;
    busy = true;
    err.textContent = "";
    try {
      const r = await request("/api/worker/link", {
        method: "POST",
        body: { join_code: code.value, label: label.value || (STATION ? "Pack station" : "Phone") },
      });
      app.device = { token: r.device_token, warehouseName: r.warehouse.name, label: r.device.label };
      writeJson(LS_DEVICE, app.device);
      history.replaceState(null, "", location.pathname);
      startSync();
      toast(T("linkDone", { warehouse: r.warehouse.name }), "ok");
      showPin();
    } catch (ex) {
      err.textContent = ex.isNetwork ? T("pinNeedsNetwork") : ex.message;
    } finally {
      busy = false;
    }
  };
  setScreen("link",
    topbar(langToggle()),
    h("main", { class: "screen narrow" },
      h("h1", null, T("linkTitle")),
      h("p", { class: "muted" }, T("linkHelp")),
      h("form", { class: "stack", onsubmit: submit },
        h("label", { for: "code" }, T("linkCodeLabel")), code,
        h("label", { for: "label" }, T("linkLabelLabel")), label,
        err,
        h("button", { class: "btn btn-primary btn-xl", type: "submit" }, T("linkButton")),
        h("button", {
          class: "btn btn-xl", type: "button",
          onclick: () => scanOnce((text) => {
            code.value = codeFromText(text);
            submit();
          }),
        }, T("linkScanButton")))));
  if (prefill) submit();
}

// ---------------------------------------------------------------------------
// PIN
// ---------------------------------------------------------------------------

function showPin(message) {
  let pin = "";
  let busy = false;
  const dots = h("div", { class: "pin-dots", "aria-hidden": "true" });
  const err = h("p", { class: "form-error", role: "alert" }, message || "");
  const paint = () => {
    mount(dots, ...[0, 1, 2, 3].map((i) => h("span", { class: ["pin-dot", i < pin.length && "filled"] })));
  };
  const submit = async () => {
    busy = true;
    try {
      const r = await api("/api/worker/login", { method: "POST", body: { pin }, noSession: true });
      app.session = {
        token: r.session_token, id: r.session_id, workerId: r.worker.id, workerName: r.worker.name,
        expiresAt: r.expires_at,
      };
      writeJson(LS_SESSION, app.session);
      app.locked = null;
      app.noticeVersion = r.notice_version;
      if (r.notice_required) showNotice(r.notice_version);
      else afterLogin();
    } catch (ex) {
      if (ex.status === 402) return;
      pin = "";
      paint();
      dots.classList.add("shake");
      setTimeout(() => dots.classList.remove("shake"), 400);
      err.textContent = ex.isNetwork ? T("pinNeedsNetwork") : ex.code === "pin_invalid" ? T("pinWrong") : ex.message;
      FX.play("bad");
    } finally {
      busy = false;
    }
  };
  const press = (d) => {
    FX.unlockAudio();
    if (busy) return;
    if (d === "clear") pin = "";
    else if (d === "back") pin = pin.slice(0, -1);
    else if (pin.length < 4) pin += d;
    err.textContent = "";
    paint();
    if (pin.length === 4) submit();
  };
  const key = (d, label, cls) =>
    h("button", { class: ["key", cls], type: "button", onclick: () => press(d), "aria-label": label || d }, label || d);
  paint();
  setScreen("pin",
    topbar(langToggle()),
    h("main", { class: "screen narrow center" },
      h("h1", null, T("pinTitle")),
      h("p", { class: "muted" }, T("pinHelp", { warehouse: app.device ? app.device.warehouseName : "" })),
      dots,
      err,
      h("div", { class: "keypad" },
        ..."123456789".split("").map((d) => key(d)),
        key("clear", T("pinClear"), "key-fn"),
        key("0"),
        key("back", "⌫", "key-fn"))));
  app.pinKeyHandler = (e) => {
    if (app.screen !== "pin") return;
    if (/^[0-9]$/.test(e.key)) press(e.key);
    else if (e.key === "Backspace") press("back");
  };
}

// ---------------------------------------------------------------------------
// Privacy notice: what Autorack records about the worker, once per worker
// ---------------------------------------------------------------------------

function showNotice(version) {
  let busy = false;
  const err = h("p", { class: "form-error", role: "alert" });
  const warehouse = app.device ? app.device.warehouseName : "";
  setScreen("notice",
    topbar(langToggle()),
    h("main", { class: "screen narrow notice" },
      h("h1", null, T("noticeTitle")),
      h("p", null, T("noticeIntro", { name: app.session ? app.session.workerName : "", warehouse })),
      h("ul", { class: "notice-list" },
        h("li", null, T("noticeName")),
        h("li", null, T("noticeScans")),
        h("li", null, T("noticeProblems")),
        h("li", null, T("noticePhone"))),
      h("p", null, T("noticeWho", { warehouse })),
      h("p", null, T("noticeNot")),
      h("p", { class: "muted small" }, T("noticeQuestions", { warehouse }), " ",
        h("a", { href: "/privacy.html", target: "_blank", rel: "noopener" }, T("noticePolicy"))),
      err,
      h("button", {
        class: "btn btn-primary btn-xl",
        onclick: async () => {
          if (busy) return;
          busy = true;
          err.textContent = "";
          try {
            await api("/api/worker/notice", { method: "POST", body: { version } });
            afterLogin();
          } catch (e) {
            err.textContent = e.isNetwork ? T("pinNeedsNetwork") : e.message;
          } finally {
            busy = false;
          }
        },
      }, T("noticeAccept"))));
}

// ---------------------------------------------------------------------------
// Orders
// ---------------------------------------------------------------------------

async function cachedOrderList() {
  return (await store.metaGet("orderList")) || [];
}

async function showOrders() {
  if (!app.session) return showPin();
  stopCamera();
  app.order = null;
  app.batch = null;
  const listEl = h("div", { class: "order-list" }, h("div", { class: "skeleton" }));
  const note = h("p", { class: "muted small" });
  const clockLine = h("div");
  const restockHost = h("div");
  setScreen("orders",
    topbar(),
    h("main", { class: "screen" },
      h("div", { class: "row-between" },
        h("h1", null, T("ordersGreeting", { name: app.session.workerName })),
        h("div", { class: "row" }, langToggle(),
          h("button", { class: "btn btn-sm", "aria-label": T("settingsTitle"), onclick: showSettings }, "⚙"),
          h("button", { class: "btn btn-sm", onclick: endShift }, T("endShift")))),
      clockLine,
      STATION
        ? h("section", { class: "station-scan" },
          h("div", { class: "scanner-ready" }, h("span", { class: "scanner-dot" }), T("stationHome")),
          h("p", { class: "muted" }, T("stationHelp")),
          h("button", { class: "btn btn-lg", onclick: typeOrderNumber }, T("ordersTypeNumber")))
        : h("div", { class: "grid-2" },
          h("button", { class: "btn btn-primary btn-xl", onclick: () => scanOnce(openFromCode) }, T("ordersScanSheet")),
          h("button", { class: "btn btn-xl", onclick: typeOrderNumber }, T("ordersTypeNumber"))),
      h("button", { class: "btn btn-lg btn-block", onclick: startReturn }, T("returnStart")),
      restockHost,
      note,
      listEl));

  let orders;
  let toShip = [];
  let batches = [];
  let offline = false;
  try {
    const r = await api("/api/worker/orders");
    orders = r.orders;
    toShip = r.to_ship || [];
    batches = r.batches || [];
    app.shift = r.shift || null;
    app.timeClock = Boolean(r.time_clock_enabled);
    mount(clockLine, app.shift
      ? h("p", { class: "muted small clock-line" }, "⏱ ", T("clockedIn", { time: fmtTime(app.shift.clock_in) }))
      : null);
    mount(restockHost, r.restock_open
      ? h("button", { class: "btn btn-lg btn-block restock-btn", onclick: showRestock }, "📦 ", T("restockCount", { n: r.restock_open }))
      : null);
    await store.metaSet("orderList", orders);
    await store.metaSet("batchList", batches);
  } catch (e) {
    if (!e.isNetwork) return;
    offline = true;
    orders = await cachedOrderList();
    batches = (await store.metaGet("batchList").catch(() => null)) || [];
  }
  if (app.screen !== "orders") return;
  note.textContent = offline ? T("ordersOfflineList") : "";
  const cached = new Map((await store.allOrders()).map((o) => [o.id, o]));
  renderOrderList(listEl, orders, cached, toShip, batches);
  if (!offline) prefetch(orders, cached).then(async () => {
    if (app.screen !== "orders") return;
    renderOrderList(listEl, orders, new Map((await store.allOrders()).map((o) => [o.id, o])), toShip, batches);
  });
}

function fmtTime(iso) {
  try {
    return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  } catch {
    return "";
  }
}

/** "3:00 PM" today, else "Tue 3:00 PM". */
function fmtDue(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const today = new Date().toDateString() === d.toDateString();
  return today ? fmtTime(iso) : `${d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })} ${fmtTime(iso)}`;
}

function batchRows(batches) {
  if (!batches.length) return [];
  return [
    h("h2", { class: "section-title" }, T("batchesTitle")),
    ...batches.map((b) => {
      const pct = b.units_expected ? Math.min(100, Math.round((100 * b.units_scanned) / b.units_expected)) : 0;
      return h("button", { class: "order-row order-row-batch", onclick: () => openBatch(b.id) },
        h("div", { class: "order-row-main" },
          h("span", { class: "badge badge-kind badge-kind-batch" }, "▦"),
          h("span", { class: "order-number" }, T("batchTitle", { number: b.number })),
          b.assigned_to_me ? h("span", { class: "badge badge-assigned" }, T("ordersAssigned")) : null),
        h("div", { class: "order-row-sub" },
          h("span", null, T("batchOrders", { n: b.order_count })),
          h("span", null, T("progressUnits", { done: b.units_scanned, total: b.units_expected }))),
        h("div", { class: "bar" }, h("span", { style: { width: `${pct}%` } })));
    }),
    h("h2", { class: "section-title" }, T("ordersTitle")),
  ];
}

function renderOrderList(listEl, orders, cached, toShip = [], batches = []) {
  const shipRows = toShip.length
    ? [
      h("h2", { class: "section-title" }, T("ordersToShip"), " · ", String(toShip.length)),
      ...toShip.map((o) => h("button", { class: "order-row order-row-ship", onclick: () => openOrder(o.id) },
        h("div", { class: "order-row-main" },
          h("span", { class: "order-number" }, o.external_order_number || o.id.slice(0, 8)),
          h("span", { class: "badge badge-completed" }, T("status_completed"))),
        h("div", { class: "order-row-sub" }, h("span", null, T("shipScan"))))),
      h("h2", { class: "section-title" }, T("ordersTitle")),
    ]
    : [];
  const batchEls = batchRows(batches);
  if (!orders.length) {
    mount(listEl, ...shipRows, ...batchEls, batches.length ? null : h("p", { class: "empty" }, T("ordersEmpty")));
    return;
  }
  mount(listEl, ...shipRows, ...batchEls, ...orders.map((o) => {
    const c = cached.get(o.id);
    const ready = c && c.version >= o.version;
    const tally = o.kind && o.kind !== "pick";
    const pct = o.units_expected ? Math.min(100, Math.round((100 * o.units_scanned) / o.units_expected)) : 0;
    return h("button", { class: ["order-row", tally && `order-row-${o.kind}`], onclick: () => openOrder(o.id) },
      h("div", { class: "order-row-main" },
        o.rush ? h("span", { class: "badge badge-rush" }, T("rushTag")) : null,
        tally ? h("span", { class: `badge badge-kind badge-kind-${o.kind}` }, T(`kind_${o.kind}`)) : null,
        h("span", { class: "order-number" }, o.external_order_number || o.id.slice(0, 8)),
        h("span", { class: `badge badge-${o.status}` }, T(`status_${o.status}`)),
        o.assigned_to_me ? h("span", { class: "badge badge-assigned" }, T("ordersAssigned")) : null),
      o.due_at ? h("div", { class: ["order-row-due", o.late && "late"] },
        o.late ? `⚠ ${T("lateTag")} · ` : "", T("dueAt", { time: fmtDue(o.due_at) })) : null,
      h("div", { class: "order-row-sub" },
        h("span", null, tally && !o.units_expected
          ? T("tallyCounted", { n: o.units_scanned })
          : T("progressUnits", { done: o.units_scanned, total: o.units_expected })),
        h("span", { class: ready ? "ok-text" : "muted" }, ready ? T("ordersReady") : T("ordersNotCached"))),
      h("div", { class: "bar" }, h("span", { style: { width: `${pct}%` } })));
  }));
}

/** Download orders ahead of time so they can be picked in a dead zone. */
async function prefetch(orders, cached) {
  for (const o of orders.slice(0, PREFETCH_LIMIT)) {
    const c = cached.get(o.id);
    if (c && c.version >= o.version) continue;
    try {
      await store.putOrder(await api(`/api/worker/orders/${o.id}`));
    } catch {
      return;
    }
  }
}

async function fetchOrder(id) {
  const payload = await api(`/api/worker/orders/${id}`);
  await store.putOrder(payload);
  return payload;
}

async function openOrder(id) {
  let order = await store.getOrder(id).catch(() => null);
  if (!order || navigator.onLine) {
    try {
      order = await fetchOrder(id);
    } catch (e) {
      if (!order) {
        toast(e.isNetwork ? T("orderNotCached") : e.message, "bad");
        return;
      }
    }
  }
  if (order.status === "cancelled") {
    toast(T("orderCancelled"), "bad");
    return;
  }
  if (order.status === "shipped") {
    toast(T("orderShipped"), "warn");
    return;
  }
  if (S.isTally(order) && (order.status === "completed" || order.finished_locally)) {
    toast(T("taskFinished"), "warn");
    return;
  }
  app.order = order;
  app.batch = null;
  app.targetLineId = null;
  app.pending = await store.outboxAll().catch(() => []);
  await countPackShots();
  showPick();
  if (navigator.onLine) refreshOrderInBackground(order.id);
}

/** Box photos still on the phone, per order. */
async function countPackShots() {
  const shots = {};
  for (const p of await store.photosAll().catch(() => [])) {
    if (p.kind === "pack") shots[p.order_id] = (shots[p.order_id] || 0) + 1;
  }
  app.packShots = shots;
}

// ---------------------------------------------------------------------------
// Batch picking: several orders in one walk, one tote each
// ---------------------------------------------------------------------------

async function openBatch(id) {
  let batch = null;
  try {
    const r = await api(`/api/worker/batches/${id}`);
    for (const o of r.orders) await store.putOrder(o);
    batch = { id: r.id, number: r.number, orders: r.orders };
    await store.metaSet(`batch:${id}`, { id: r.id, number: r.number, orderIds: r.orders.map((o) => o.id) });
  } catch (e) {
    if (!e.isNetwork) {
      toast(e.message, "bad");
      return;
    }
    const meta = await store.metaGet(`batch:${id}`).catch(() => null);
    const orders = meta ? await Promise.all(meta.orderIds.map((oid) => store.getOrder(oid).catch(() => null))) : [];
    if (!meta || orders.some((o) => !o)) {
      toast(T("batchNotCached"), "bad");
      return;
    }
    batch = { id: meta.id, number: meta.number, orders };
  }
  app.batch = batch;
  app.pending = await store.outboxAll().catch(() => []);
  app.targetLineId = null;
  app.order = batchOrders()[0] || batch.orders[0];
  await countPackShots();
  showPick();
}

/** The batch's orders still being picked, in tote order. */
function batchOrders() {
  return app.batch.orders.filter((o) => !["cancelled", "shipped"].includes(o.status));
}

/** Every line of the batch, each tagged with its order and tote. */
function batchLines() {
  return batchOrders().flatMap((o) =>
    S.displayLines(o, pendingFor(o.id)).map((l) => ({ ...l, orderId: o.id, tote: o.tote })));
}

function batchOrder(id) {
  return app.batch.orders.find((o) => o.id === id) || null;
}

/** Point app.order at the order of the line being picked (for short picks, flags). */
function focusBatchTarget() {
  if (!app.batch) return;
  const target = S.nextLine(batchLines(), app.targetLineId);
  if (target) app.order = batchOrder(target.orderId);
}

function renderBatchBody() {
  const lines = batchLines();
  const prog = S.progress(lines);
  const target = S.nextLine(lines, app.targetLineId);
  const pct = prog.total ? Math.round((100 * prog.done) / prog.total) : 0;
  const flagged = batchOrders().some((o) => o.status === "flagged" || o.open_flags);
  mount(document.getElementById("pick-top"),
    h("div", { class: "pick-head" },
      h("button", { class: "btn btn-sm", onclick: showOrders }, "← ", T("back")),
      h("div", { class: "pick-title" },
        h("span", { class: "order-number" }, T("batchTitle", { number: app.batch.number })),
        h("span", { class: "muted" }, T("progressUnits", { done: prog.done, total: prog.total })))),
    h("div", { class: "bar bar-lg" }, h("span", { style: { width: `${pct}%` } })),
    flagged ? h("div", { class: "banner banner-warn" }, T("orderFlagged")) : null,
    app.locked ? h("div", { class: "banner banner-bad" }, app.locked) : null);

  const targetCard = target
    ? h("section", { class: ["target", productOf(target) && productOf(target).thumb && "target-with-photo"] },
      productPhoto(target),
      h("div", { class: "target-label" }, T("pickNext")),
      target.location ? h("div", { class: "target-location" }, target.location) : null,
      h("div", { class: "target-name" }, lineLabel(target)),
      h("div", { class: "tote-tag" }, T("batchTote", { tote: target.tote || "?" })),
      target.kit_name ? h("div", { class: "kit-tag" }, T("kitPart", { kit: target.kit_name })) : null,
      packerNote(target),
      h("div", { class: "target-meta" },
        target.sku ? h("span", { class: "mono" }, target.sku) : null,
        h("span", { class: "mono muted" }, target.expected_barcode)),
      traceTags(target),
      target.confirm_without_scan ? confirmButton(target) : null,
      h("div", { class: "target-qty" },
        T("pickQty", { done: target.scanned_quantity, total: target.expected_quantity }),
        target.short_quantity ? h("span", { class: "short-tag" }, T("shortLine", { n: target.short_quantity })) : null))
    : h("section", { class: "target target-done" },
      h("div", { class: "complete-check" }, "✓"),
      h("div", { class: "target-name" }, T("batchDone")),
      h("p", { class: "muted" }, T("batchDoneDetail")),
      h("button", { class: "btn btn-primary btn-xl", onclick: showOrders }, T("orderCompleteNext")));

  const withTarget = (fn) => () => {
    focusBatchTarget();
    return fn(null);
  };
  mount(document.getElementById("pick-body"),
    targetCard,
    target
      ? h("div", { class: "actions" },
        scanButton(),
        h("button", { class: "btn btn-lg", onclick: typeBarcode }, T("pickType")),
        h("button", { class: "btn btn-lg", onclick: withTarget(shortPick) }, T("pickShort")),
        h("button", { class: "btn btn-lg", onclick: withTarget(flagProblem) }, T("pickFlag")),
        h("button", { class: "btn btn-lg", onclick: undoLast }, T("pickUndo")),
        h("button", { class: "btn btn-lg", onclick: () => reportEmptyBin(target) }, T("pickBinEmpty")))
      : h("div", { class: "actions" },
        h("button", { class: "btn btn-lg", onclick: undoLast }, T("pickUndo"))),
    h("h2", { class: "section-title" }, T("pickAllLines")),
    h("ul", { class: "lines" }, ...S.sortForWalking(lines).map((l) => {
      const done = S.remaining(l) === 0;
      return h("li", {
        class: ["line", done && "line-done", target && l.id === target.id && "line-target"],
        onclick: () => {
          if (done) return;
          app.targetLineId = l.id;
          renderPickBody();
        },
      },
      h("div", { class: "line-main" },
        h("span", { class: "line-name" }, productPhoto(l, "line-photo"), lineLabel(l)),
        h("span", { class: "line-qty" }, done ? `✓ ${l.expected_quantity}` : `${l.scanned_quantity}/${l.expected_quantity}`)),
      h("div", { class: "line-sub" },
        h("span", { class: "tote-chip" }, l.tote || "?"),
        l.location ? h("span", null, l.location) : null,
        h("span", { class: "mono" }, l.expected_barcode)));
    })));
}

async function openFromCode(text) {
  const id = S.orderIdFromCode(text);
  if (id) return openOrder(id);
  try {
    const r = await api(`/api/worker/orders/lookup?code=${encodeURIComponent(text)}`);
    return openOrder(r.order_id);
  } catch (e) {
    if (!e.isNetwork) {
      toast(T("orderNotFound"), "bad");
      return;
    }
  }
  const wanted = text.trim().toUpperCase();
  const hit = (await store.allOrders()).find((o) => (o.external_order_number || "").toUpperCase() === wanted);
  if (hit) return openOrder(hit.id);
  toast(T("orderNotCached"), "bad");
}

function typeOrderNumber() {
  dialog(T("orderLookupPrompt"), (close) => {
    const input = h("input", { class: "input input-xl", autocomplete: "off", autocapitalize: "characters" });
    return h("form", {
      class: "stack",
      onsubmit: (e) => {
        e.preventDefault();
        close(input.value.trim());
      },
    }, input, h("div", { class: "dialog-actions" },
      h("button", { class: "btn", type: "button", onclick: () => close(null) }, T("cancel")),
      h("button", { class: "btn btn-primary", type: "submit" }, T("manualSubmit"))));
  }).then((v) => v && openFromCode(v));
}

// ---------------------------------------------------------------------------
// Picking
// ---------------------------------------------------------------------------

function pendingFor(orderId) {
  return app.pending.filter((e) => e.order_id === orderId);
}

function currentLines() {
  return S.displayLines(app.order, pendingFor(app.order.id));
}

function lineLabel(l) {
  return l.description || l.sku || l.expected_barcode;
}

function showPick() {
  if (!app.order) return showOrders();
  // The camera panel lives outside the re-rendered region: rebuilding it on
  // every scan would restart the video stream each time.
  if (app.screen !== "pick" || !document.getElementById("pick-body")) {
    setScreen("pick",
      topbar(),
      h("main", { class: "screen pick" },
        h("div", { id: "pick-top" }),
        h("div", { class: "camera", id: "camera", hidden: true },
          h("video", { id: "video", playsinline: true, muted: true, autoplay: true }),
          h("canvas", { id: "canvas", hidden: true }),
          h("div", { class: "camera-frame" }),
          h("div", { class: "camera-bar" },
            h("span", { id: "camera-countdown" }),
            h("button", { class: "btn btn-sm", id: "torch", hidden: true, onclick: toggleTorch }, "💡"),
            h("button", { class: "btn btn-sm", onclick: stopCamera }, "✕"))),
        h("div", { id: "pick-body" })));
    if (app.camera) attachCamera();
  }
  renderPickBody();
}

function renderPickBody() {
  if (app.batch) return renderBatchBody();
  if (S.isTally(app.order)) return renderTallyBody();
  const order = app.order;
  const lines = currentLines();
  const prog = S.progress(lines);
  const target = S.nextLine(lines, app.targetLineId);
  const pct = prog.total ? Math.round((100 * prog.done) / prog.total) : 0;

  mount(document.getElementById("pick-top"),
    h("div", { class: "pick-head" },
      h("button", { class: "btn btn-sm", onclick: showOrders }, "← ", T("back")),
      h("div", { class: "pick-title" },
        h("span", { class: "order-number" }, order.external_order_number || order.id.slice(0, 8)),
        h("span", { class: "muted" }, T("progressUnits", { done: prog.done, total: prog.total })))),
    h("div", { class: "bar bar-lg" }, h("span", { style: { width: `${pct}%` } })),
    order.status === "flagged" || order.open_flags ? h("div", { class: "banner banner-warn" }, T("orderFlagged")) : null,
    order.rush || order.late ? h("div", { class: ["banner", order.late ? "banner-bad" : "banner-warn"] },
      order.rush ? `${T("rushTag")} · ` : "", order.due_at ? T("dueAt", { time: fmtDue(order.due_at) }) : "") : null,
    order.notes ? h("div", { class: "banner banner-note" }, h("strong", null, T("orderNote"), ": "), order.notes) : null,
    app.locked ? h("div", { class: "banner banner-bad" }, app.locked) : null);

  const targetCard = target
    ? h("section", { class: ["target", productOf(target) && productOf(target).thumb && "target-with-photo"] },
      productPhoto(target),
      h("div", { class: "target-label" }, T("pickNext")),
      target.location ? h("div", { class: "target-location" }, target.location) : null,
      h("div", { class: "target-name" }, lineLabel(target)),
      target.kit_name ? h("div", { class: "kit-tag" }, T("kitPart", { kit: target.kit_name })) : null,
      packerNote(target),
      h("div", { class: "target-meta" },
        target.sku ? h("span", { class: "mono" }, target.sku) : null,
        h("span", { class: "mono muted" }, target.expected_barcode)),
      traceTags(target),
      target.confirm_without_scan ? confirmButton(target) : null,
      h("div", { class: "target-qty" },
        T("pickQty", { done: target.scanned_quantity, total: target.expected_quantity }),
        target.short_quantity ? h("span", { class: "short-tag" }, T("shortLine", { n: target.short_quantity })) : null))
    : h("section", { class: "target target-done" },
      h("div", { class: "target-name" }, T("orderComplete")),
      ...shipControls());

  mount(document.getElementById("pick-body"),
    targetCard,
    target
      ? h("div", { class: "actions" },
        scanButton(),
        h("button", { class: "btn btn-lg", onclick: typeBarcode }, T("pickType")),
        h("button", { class: "btn btn-lg", onclick: shortPick }, T("pickShort")),
        h("button", { class: "btn btn-lg", onclick: () => flagProblem(null) }, T("pickFlag")),
        h("button", { class: "btn btn-lg", onclick: undoLast }, T("pickUndo")),
        h("button", { class: "btn btn-lg", onclick: () => reportEmptyBin(target) }, T("pickBinEmpty")))
      : h("div", { class: "actions" },
        h("button", { class: "btn btn-lg", onclick: () => flagProblem(null) }, T("pickFlag")),
        h("button", { class: "btn btn-lg", onclick: undoLast }, T("pickUndo"))),
    h("h2", { class: "section-title" }, T("pickAllLines")),
    h("ul", { class: "lines" }, ...S.sortForWalking(lines).map((l) => {
      const done = S.remaining(l) === 0;
      const short = l.short_quantity || 0;
      return h("li", {
        class: ["line", done && "line-done", target && l.id === target.id && "line-target"],
        onclick: () => {
          if (done) return;
          app.targetLineId = l.id;
          renderPickBody();
        },
      },
      h("div", { class: "line-main" },
        h("span", { class: "line-name" }, productPhoto(l, "line-photo"), lineLabel(l)),
        h("span", { class: "line-qty" },
          done && !short ? `✓ ${l.expected_quantity}` : `${l.scanned_quantity}/${l.expected_quantity}`)),
      h("div", { class: "line-sub" },
        short ? h("span", { class: "short-tag" }, T("shortLine", { n: short })) : null,
        l.confirm_without_scan ? h("span", { class: "short-tag" }, T("confirmTag")) : null,
        l.location ? h("span", null, l.location) : null,
        h("span", { class: "mono" }, l.expected_barcode)));
    })));
}

// ---------------------------------------------------------------------------
// Lot / serial / expiry prompt
// ---------------------------------------------------------------------------

/** Ask for the details this line needs. A scanner can type into the focused
 * field; the camera button reads a lot/serial/expiry barcode. */
async function askDetails(line, need) {
  const paused = app.camera && app.camera.scanner;
  if (paused) app.camera.scanner.pause();
  try {
    return await dialog(T("detailsTitle"), (close) => {
      const inputs = {};
      const fields = need.map((f) => {
        const input = f === "expiry"
          ? h("input", { class: "input input-xl", type: "date", required: true })
          : h("input", { class: "input input-xl mono", autocomplete: "off", autocapitalize: "characters", required: true, maxlength: "100" });
        inputs[f] = input;
        const cam = f === "expiry" ? null : h("button", {
          class: "btn", type: "button", "aria-label": T("pickCamera"),
          onclick: () => scanOnce((text) => {
            const g = S.unitDetails(text);
            input.value = (f === "lot" ? g.lot : g.serial) || text.trim();
          }),
        }, "📷");
        return h("div", { class: "stack" }, h("label", null, T(`details_${f}`)), h("div", { class: "detail-row" }, input, cam));
      });
      return h("form", {
        class: "stack",
        onsubmit: (e) => {
          e.preventDefault();
          const out = {};
          for (const [f, el] of Object.entries(inputs)) out[f] = el.value.trim() || null;
          if (Object.values(out).some((v) => !v)) return;
          close(out);
        },
      },
      h("p", { class: "muted" }, lineLabel(line)),
      line.required_lot ? h("p", { class: "banner banner-info" }, T("detailsNeedLot", { lot: line.required_lot })) : null,
      ...fields,
      h("div", { class: "dialog-actions" },
        h("button", { class: "btn", type: "button", onclick: () => close(null) }, T("cancel")),
        h("button", { class: "btn btn-primary", type: "submit" }, T("manualSubmit"))));
    });
  } finally {
    if (paused && app.camera && app.camera.scanner) app.camera.scanner.resume();
  }
}

// ---------------------------------------------------------------------------
// Receiving, returns, counts: scan everything, then finish
// ---------------------------------------------------------------------------

function renderTallyBody() {
  const order = app.order;
  const lines = currentLines();
  const blind = Boolean(order.blind);
  const extras = S.extrasFor(app.history, order.id);
  const counted = lines.reduce((n, l) => n + l.scanned_quantity, 0);
  const expected = lines.reduce((n, l) => n + l.expected_quantity, 0);
  const pct = !blind && expected ? Math.min(100, Math.round((100 * counted) / expected)) : 0;

  mount(document.getElementById("pick-top"),
    h("div", { class: "pick-head" },
      h("button", { class: "btn btn-sm", onclick: showOrders }, "← ", T("back")),
      h("div", { class: "pick-title" },
        h("span", { class: `badge badge-kind badge-kind-${order.kind}` }, T(`kind_${order.kind}`)),
        h("span", { class: "order-number" }, order.external_order_number || order.id.slice(0, 8)),
        h("span", { class: "muted" }, blind ? T("tallyCounted", { n: counted }) : T("progressUnits", { done: counted, total: expected })))),
    blind ? null : h("div", { class: "bar bar-lg" }, h("span", { style: { width: `${pct}%` } })),
    order.status === "flagged" || order.open_flags ? h("div", { class: "banner banner-warn" }, T("orderFlagged")) : null,
    app.locked ? h("div", { class: "banner banner-bad" }, app.locked) : null);

  mount(document.getElementById("pick-body"),
    h("section", { class: "target target-tally" },
      h("div", { class: "target-label" }, T(`tallyPrompt_${order.kind}`)),
      h("div", { class: "tally-big" }, String(counted)),
      h("div", { class: "target-meta" }, T("tallyItemsCounted"),
        extras ? h("span", { class: "short-tag" }, T("tallyExtras", { n: extras })) : null),
      order.kind === "return" && order.customer ? h("div", { class: "muted" }, order.customer) : null),
    h("div", { class: "actions" },
      scanButton(),
      h("button", { class: "btn btn-lg", onclick: typeBarcode }, T("pickType")),
      h("button", { class: "btn btn-lg", onclick: () => flagProblem(null) }, T("pickFlag")),
      h("button", { class: "btn btn-lg", onclick: undoLast }, T("pickUndo"))),
    h("button", { class: "btn btn-xl btn-block btn-finish", onclick: finishTask }, T("tallyFinish")),
    h("h2", { class: "section-title" }, blind ? T("tallyListBlind") : T("pickAllLines")),
    h("ul", { class: "lines" }, ...S.sortForWalking(lines).map((l) => {
      const state = blind ? null : l.scanned_quantity === l.expected_quantity ? "ok" : l.scanned_quantity > l.expected_quantity ? "over" : null;
      return h("li", { class: ["line", state === "ok" && "line-done", state === "over" && "line-over"] },
        h("div", { class: "line-main" },
          h("span", { class: "line-name" }, productPhoto(l, "line-photo"), lineLabel(l)),
          h("span", { class: "line-qty" }, blind ? String(l.scanned_quantity) : `${l.scanned_quantity}/${l.expected_quantity}`)),
        h("div", { class: "line-sub" },
          l.location ? h("span", null, l.location) : null,
          lineLabel(l) !== l.expected_barcode ? h("span", { class: "mono" }, l.expected_barcode) : null),
        l.confirm_without_scan ? confirmButton(l, true) : null);
    })));
}

// ---------------------------------------------------------------------------
// Items with no barcode: confirmed by tap, marked as not scan-verified
// ---------------------------------------------------------------------------

function confirmButton(line, small = false) {
  return h("button", {
    class: ["btn", small ? "btn-sm" : "btn-primary btn-lg btn-block", "confirm-btn"],
    onclick: (e) => {
      e.stopPropagation();
      confirmNoBarcode(line);
    },
  }, "✋ ", T("confirmTap"));
}

async function confirmNoBarcode(line) {
  if (app.batch && line.orderId) app.order = batchOrder(line.orderId);
  const order = app.order;
  const tally = S.isTally(order);
  const max = tally ? 100000 : S.remaining(line);
  if (max <= 0) return;
  const photos = [];
  const qty = await dialog(T("confirmTitle"), (close) => {
    const input = h("input", {
      class: "input input-xl", type: "number", inputmode: "numeric", min: "1", max: String(max),
      value: String(tally ? 1 : max),
    });
    return h("form", {
      class: "stack",
      onsubmit: (e) => {
        e.preventDefault();
        close(Math.max(1, Math.min(max, parseInt(input.value, 10) || 1)));
      },
    },
    h("p", { class: "confirm-item" }, productPhoto(line, "line-photo"), h("strong", null, lineLabel(line))),
    h("p", { class: "muted" }, T("confirmHelp")),
    h("label", null, T("confirmQty")), input,
    photoPicker(photos),
    h("div", { class: "dialog-actions" },
      h("button", { class: "btn", type: "button", onclick: () => close(null) }, T("cancel")),
      h("button", { class: "btn btn-primary", type: "submit" }, T("confirmButton"))));
  });
  if (!qty) return;
  const result = tally ? "counted" : "match";
  const ev = {
    id: uuid4(),
    kind: "confirm",
    order_id: order.id,
    session_id: app.session.id,
    client_scanned_at: new Date().toISOString(),
    client_seq: await store.nextSeq().catch(() => Date.now()),
    line_item_id: line.id,
    quantity: qty,
    offline: !app.status.online,
    local: { result, lineId: line.id, qty },
  };
  try {
    await store.outboxAdd(ev);
  } catch {
    toast(T("storageFailed"), "bad");
    return;
  }
  app.pending.push(ev);
  remember({ id: ev.id, orderId: order.id, kind: "scan", result, lineId: line.id, qty, at: ev.client_scanned_at });
  await savePackPhotos(photos, order.id);
  app.status.pending += 1;
  refreshChip();
  FX.play("ok");
  toast(T("confirmDone", { n: qty }), "ok");
  if (app.sync) app.sync.kick();
  app.targetLineId = null;
  if (!tally && !app.batch && S.progress(currentLines()).complete) showComplete();
  else renderPickBody();
}

async function finishTask() {
  const order = app.order;
  const lines = currentLines();
  const sum = S.tallySummary(lines, S.extrasFor(app.history, order.id));
  const detail = order.blind
    ? [T("tallyFinishBlind", { n: sum.counted })]
    : sum.matches
      ? [T("tallyFinishMatches")]
      : [
        sum.short ? T("tallyFinishShort", { n: sum.short }) : null,
        sum.over ? T("tallyFinishOver", { n: sum.over }) : null,
        sum.extras ? T("tallyFinishExtras", { n: sum.extras }) : null,
      ].filter(Boolean);
  const ok = await dialog(T("tallyFinishTitle"), (close) => [
    h("ul", { class: "finish-summary" }, ...detail.map((d) => h("li", null, d))),
    h("p", { class: "muted" }, T("tallyFinishHelp")),
    h("div", { class: "dialog-actions" },
      h("button", { class: "btn", onclick: () => close(false) }, T("tallyKeepScanning")),
      h("button", { class: "btn btn-primary", onclick: () => close(true) }, T("tallyFinish"))),
  ]);
  if (!ok) return;
  const ev = {
    id: uuid4(),
    kind: "finish",
    order_id: order.id,
    session_id: app.session.id,
    client_scanned_at: new Date().toISOString(),
    client_seq: await store.nextSeq().catch(() => Date.now()),
    offline: !app.status.online,
  };
  try {
    await store.outboxAdd(ev);
  } catch {
    toast(T("storageFailed"), "bad");
    return;
  }
  app.pending.push(ev);
  await store.putOrder({ ...order, finished_locally: true }).catch(() => {});
  app.status.pending += 1;
  refreshChip();
  if (app.sync) app.sync.kick();
  FX.play("ok");
  toast(T("tallyFinished"), "ok");
  showOrders();
}

function startReturn() {
  dialog(T("returnStartTitle"), (close) => {
    const input = h("input", { class: "input input-xl", autocomplete: "off", autocapitalize: "characters", placeholder: T("returnStartPlaceholder") });
    return h("form", {
      class: "stack",
      onsubmit: (e) => {
        e.preventDefault();
        close({ code: input.value.trim() });
      },
    },
    h("p", { class: "muted" }, T("returnStartHelp")),
    h("button", { class: "btn btn-primary btn-lg", type: "button", onclick: () => close({ scan: true }) }, T("returnScanLabel")),
    input,
    h("div", { class: "dialog-actions" },
      h("button", { class: "btn", type: "button", onclick: () => close(null) }, T("cancel")),
      h("button", { class: "btn btn-primary", type: "submit" }, T("manualSubmit"))));
  }).then((v) => {
    if (!v) return;
    if (v.scan) scanOnce(beginReturn);
    else if (v.code) beginReturn(v.code);
  });
}

async function beginReturn(code) {
  try {
    const r = await api("/api/worker/returns", { method: "POST", body: { code: String(code).slice(0, 200) } });
    toast(T("returnStarted", { number: r.number }), "ok");
    await openOrder(r.order_id);
  } catch (e) {
    toast(e.isNetwork ? T("returnNeedsNetwork") : e.message, "bad", 8000);
  }
}

/** The catalog product behind a line (picture, packer note), if linked. */
function productOf(line) {
  return line && line.product_id && app.order && app.order.products ? app.order.products[line.product_id] : null;
}

function productPhoto(line, cls = "target-photo") {
  const p = productOf(line);
  return p && p.thumb ? h("img", { class: cls, src: p.thumb, alt: "" }) : null;
}

function packerNote(line) {
  const p = productOf(line);
  return p && p.packer_note ? h("div", { class: "packer-note" }, "⚠ ", p.packer_note) : null;
}

/** "Lot A100 only · Serial · Exp": what this line records for each unit. */
function traceTags(line) {
  const tags = [
    line.required_lot ? T("tagLotOnly", { lot: line.required_lot }) : line.track_lot ? T("tagLot") : null,
    line.track_serial ? T("tagSerial") : null,
    line.track_expiry ? T("tagExpiry") : null,
  ].filter(Boolean);
  return tags.length ? h("div", { class: "trace-tags" }, ...tags.map((t) => h("span", { class: "short-tag" }, t))) : null;
}

async function refreshOrderInBackground(id) {
  try {
    const fresh = await api(`/api/worker/orders/${id}`);
    const before = await store.getOrder(id);
    await store.putOrder(fresh);
    if (app.order && app.order.id === id && before && fresh.version !== before.version) {
      app.order = fresh;
      if (!app.overlay && app.screen === "pick") renderPickBody();
    }
    if (fresh.status === "cancelled" && app.order && app.order.id === id) {
      toast(T("orderCancelled"), "bad", 8000);
      FX.play("bad");
    }
  } catch {
    /* offline or not allowed: keep the cached copy */
  }
}

async function handleScan(rawText) {
  if (app.screen !== "pick" || !app.order) return;
  if (app.overlay) {
    // A green flash never blocks the next scan (a wedge scanner can fire
    // several a second); a red or amber one must be acknowledged first.
    if (!app.overlay.timer) return;
    app.overlay.close();
  }
  const raw = cleanScan(rawText);
  if (!raw.trim()) return;
  if (S.isOrderCode(raw)) {
    showResult({ result: "order_code" });
    return;
  }
  let order = app.order;
  let lines = currentLines();
  let target = S.nextLine(lines, app.targetLineId);
  let c;
  if (app.batch) {
    // Which tote is it for? The first order in the batch that still needs it.
    const all = batchLines();
    const want = S.nextLine(all, app.targetLineId);
    const entries = batchOrders().map((o) => ({ order: o, lines: S.displayLines(o, pendingFor(o.id)) }));
    if (!entries.length) return;
    const pick = S.classifyBatch(entries, raw, want ? want.orderId : null);
    order = pick.order;
    c = pick.c;
    app.order = order;
    lines = currentLines();
    target = want && want.orderId === order.id ? lines.find((l) => l.id === want.id) : null;
  } else {
    c = S.classify(order, lines, raw);
  }
  // Lot / serial / expiry: from the barcode if it carries them, else ask.
  let details = S.unitDetails(raw);
  const hit = lines.find((l) => l.id === c.lineId);
  if (hit && (c.result === "match" || c.result === "counted")) {
    const need = S.neededDetails(hit, details);
    if (need.length) {
      const got = await askDetails(hit, need);
      if (!got) {
        toast(T("detailsSkipped"), "warn");
        return;
      }
      details = { ...details, ...got };
    }
    const problem = S.isTally(order) ? null : S.traceProblem(hit, details, app.history, S.localToday());
    if (problem) c = { result: "mismatch", lineId: null, tier: c.tier, problem, line: hit, details };
  }
  let seq;
  try {
    seq = await store.nextSeq();
  } catch {
    seq = Date.now();
  }
  const ev = {
    id: uuid4(),
    kind: "scan",
    order_id: order.id,
    session_id: app.session.id,
    client_scanned_at: new Date().toISOString(),
    client_seq: seq,
    scanned_barcode: raw.slice(0, 500),
    intended_line_item_id: target ? target.id : null,
    client_result: c.result,
    offline: !app.status.online,
    lot: details.lot || null,
    serial: details.serial || null,
    expiry: details.expiry || null,
    local: { result: c.result, lineId: c.lineId, qty: c.qty || 1 },
  };
  try {
    await store.outboxAdd(ev);
  } catch {
    showResult({ result: "storage_failed" });
    return;
  }
  app.pending.push(ev);
  remember({ id: ev.id, orderId: order.id, kind: "scan", result: c.result, lineId: c.lineId, qty: c.qty || 1, serial: details.serial || null, at: ev.client_scanned_at });
  const after = currentLines();
  const line = after.find((l) => l.id === c.lineId) || c.line;
  if (c.result === "match" && line && line.scanned_quantity >= line.expected_quantity) app.targetLineId = null;
  const complete = app.batch ? false : !S.isTally(order) && S.progress(after).complete;
  showResult({ ...c, line, scanId: ev.id, complete, tote: app.batch ? order.tote : null });
  app.status.pending += 1;
  refreshChip();
  if (app.sync) app.sync.kick();
}

function remember(entry) {
  app.history.push(entry);
  if (app.history.length > 100) app.history = app.history.slice(-100);
  store.metaSet("history", app.history).catch(() => {});
}

function showResult(r) {
  const order = app.order || {};
  const blind = Boolean(order.blind);
  const extraIsBad = order.kind === "return";
  const spec = {
    counted: ["ok", "✓", T("resultCounted"), r.line
      ? `${lineLabel(r.line)} · ${blind ? T("tallyCounted", { n: r.line.scanned_quantity }) : T("resultMatchDetail", { done: r.line.scanned_quantity, total: r.line.expected_quantity })}`
      : ""],
    extra: extraIsBad
      ? ["bad", "✕", T("resultNotReturned"), T("resultNotReturnedDetail")]
      : ["warn", "+", T("resultExtra"), T(order.kind === "count" ? "resultExtraDetailCount" : "resultExtraDetail")],
    match: r.sub
      ? ["warn", "⇄", T("resultSubstitute"), T("resultSubstituteDetail", { sub: r.sub, item: r.line ? lineLabel(r.line) : "" })]
      : r.tote
        ? ["ok", r.tote, T("batchPutInTote", { tote: r.tote }), r.line ? `${r.qty ? `${T("resultCase", { n: r.qty })} · ` : ""}${lineLabel(r.line)}` : ""]
        : ["ok", "✓", r.qty ? T("resultCase", { n: r.qty }) : T("resultMatch"), r.line ? `${lineLabel(r.line)} · ${T("resultMatchDetail", { done: r.line.scanned_quantity, total: r.line.expected_quantity })}` : ""],
    mismatch: r.problem === "wrong_lot"
      ? ["bad", "✕", T("resultWrongLot"), T("resultWrongLotDetail", { lot: r.line.required_lot })]
      : r.problem === "expired"
        ? ["bad", "✕", T("resultExpired"), T("resultExpiredDetail", { date: r.details.expiry })]
        : r.problem === "serial_repeat"
          ? ["bad", "✕", T("resultSerialRepeat"), T("resultSerialRepeatDetail", { serial: r.details.serial })]
          : ["bad", "✕", T("resultMismatch"), T("resultMismatchDetail")],
    over_pick: r.qty && r.line && S.remaining(r.line) > 0
      ? ["warn", "!", T("resultCaseTooBig", { n: r.qty }), T("resultCaseTooBigDetail", { left: S.remaining(r.line) })]
      : ["warn", "!", T("resultOverPick"), r.line ? `${lineLabel(r.line)} · ${T("resultOverPickDetail")}` : T("resultOverPickDetail")],
    review: ["warn", "?", T("resultReview"), T("resultReviewDetail")],
    order_code: ["warn", "!", T("resultOrderCode"), T("resultOrderCodeDetail")],
    storage_failed: ["bad", "✕", T("storageFailed"), ""],
  }[r.result];
  const [kind, icon, title, detail] = spec;
  FX.play(kind);
  if (app.camera && app.camera.scanner) app.camera.scanner.pause();
  bumpCameraIdle();
  if (r.result !== "match" || !r.complete) renderPickBody();

  const close = () => {
    if (!app.overlay) return false;
    clearTimeout(app.overlay.timer);
    app.overlay.el.remove();
    app.overlay = null;
    if (app.camera && app.camera.scanner) app.camera.scanner.resume();
    return true;
  };
  const dismiss = () => {
    if (!close()) return;
    if (r.complete) showComplete();
    else showPick();
  };
  const buttons = kind === "ok"
    ? null
    : h("div", { class: "overlay-actions" },
      r.result === "mismatch" || r.result === "review" || r.result === "extra"
        ? h("button", {
          class: "btn btn-xl btn-ghost-light",
          onclick: (e) => {
            e.stopPropagation();
            dismiss();
            flagProblem(r.scanId);
          },
        }, T("resultFlag"))
        : null,
      h("button", { class: "btn btn-xl btn-light", onclick: dismiss }, T("resultDismiss")));
  const el = h("div", {
    class: ["overlay", `overlay-${kind}`, r.tote && r.result === "match" && "overlay-tote"], role: "alertdialog", "aria-live": "assertive",
    onclick: kind === "ok" ? dismiss : null,
  },
  h("div", { class: "overlay-icon" }, icon),
  h("div", { class: "overlay-title" }, title),
  detail ? h("div", { class: "overlay-detail" }, detail) : null,
  buttons);
  document.body.appendChild(el);
  // A tote letter stays up a little longer: the worker has to read it.
  const ms = r.tote ? MATCH_OVERLAY_MS * 2 : MATCH_OVERLAY_MS;
  app.overlay = { el, timer: kind === "ok" ? setTimeout(dismiss, ms) : null, dismiss, close };
}

function showComplete() {
  if (!app.order) return showOrders();
  if (app.batch) return showPick();
  stopCamera();
  if (app.screen !== "complete") FX.play("ok");
  const prog = S.progress(currentLines());
  setScreen("complete",
    topbar(),
    h("main", { class: "screen narrow center complete" },
      h("div", { class: "complete-check" }, "✓"),
      h("h1", null, T("orderComplete")),
      h("p", { class: "muted" }, prog.short
        ? T("orderCompleteShort", { n: prog.short })
        : T("orderCompleteDetail", { order: app.order.external_order_number || "" })),
      h("div", { class: "stack" }, ...shipControls({ big: true }))));
}

// ---------------------------------------------------------------------------
// Pack and ship: the shipping label, scanned onto the order
// ---------------------------------------------------------------------------

/** Order note and product packer notes, shown where the box is packed. */
function packingNotes(order) {
  const seen = new Set();
  const notes = [];
  for (const l of order.lines || []) {
    const p = productOf(l);
    if (p && p.packer_note && !seen.has(p.packer_note)) {
      seen.add(p.packer_note);
      notes.push(h("li", null, h("strong", null, lineLabel(l)), ": ", p.packer_note));
    }
  }
  if (!order.notes && !notes.length) return null;
  return h("section", { class: "pack-notes" },
    h("div", { class: "target-label" }, T("packNotes")),
    order.notes ? h("p", { class: "pack-note-order" }, order.notes) : null,
    notes.length ? h("ul", null, ...notes) : null);
}

/** Flyers, cards, samples: each ticked off (or scanned) before the label. */
function insertChecklist(order) {
  const inserts = order.inserts || [];
  if (!inserts.length) return null;
  const done = S.insertsDone(order, pendingFor(order.id));
  return h("section", { class: "inserts" },
    h("div", { class: "target-label" }, T("insertsTitle")),
    h("ul", { class: "insert-list" }, ...inserts.map((i) => {
      const ok = done.has(i.id);
      return h("li", { class: ["insert", ok && "insert-done"] },
        h("span", { class: "insert-name" }, ok ? "✓ " : "☐ ", i.name),
        ok ? null : i.scan_required
          ? h("button", { class: "btn btn-sm", onclick: () => scanOnce((code) => handleLabel(code)) }, T("insertScan"))
          : h("button", { class: "btn btn-sm btn-primary", onclick: () => checkInsert(i, null) }, T("insertTap")));
    })));
}

async function checkInsert(insert, scanned) {
  const order = app.order;
  const ev = {
    id: uuid4(),
    kind: "insert",
    order_id: order.id,
    session_id: app.session.id,
    client_scanned_at: new Date().toISOString(),
    client_seq: await store.nextSeq().catch(() => Date.now()),
    insert_id: insert.id,
    scanned_barcode: scanned ? String(scanned).slice(0, 500) : null,
    offline: !app.status.online,
    local: {},
  };
  try {
    await store.outboxAdd(ev);
  } catch {
    toast(T("storageFailed"), "bad");
    return;
  }
  app.pending.push(ev);
  FX.play("ok");
  toast(T("insertChecked", { name: insert.name }), "ok", 2500);
  if (app.sync) app.sync.kick();
  if (app.screen === "complete") showComplete();
  else renderPickBody();
}

function shipControls({ big = false } = {}) {
  const order = app.order;
  const tracking = S.shippedTracking(order, pendingFor(order.id));
  const size = big ? "btn-xl" : "btn-lg";
  if (tracking !== null) {
    return [
      h("p", { class: "ship-done" }, "✓ ", T("shippedAlready", { tracking })),
      h("button", { class: ["btn btn-primary", size], onclick: showOrders }, T("shipNext")),
    ];
  }
  const blocked = order.open_flags > 0 || order.status === "flagged";
  const required = order.require_ship_scan;
  if (blocked) {
    const short = S.progress(currentLines()).short > 0;
    return [
      h("p", { class: "banner banner-warn" }, short ? T("orderWaitingShort") : T("shipFlagged")),
      h("button", { class: ["btn btn-primary", size], onclick: showOrders }, T("orderCompleteNext")),
    ];
  }
  const shots = packShotCount(order);
  const needPhoto = Boolean(order.require_pack_photo) && shots === 0;
  const inserts = order.inserts || [];
  const done = S.insertsDone(order, pendingFor(order.id));
  const needInserts = inserts.some((i) => !done.has(i.id));
  const boxes = S.boxesLabelled(order, pendingFor(order.id));
  const multi = Boolean(app.multiBox[order.id]) || boxes.count > 0;
  const blockedLabel = needPhoto || needInserts;
  const multiToggle = h("input", {
    type: "checkbox",
    onchange: (e) => {
      app.multiBox[order.id] = e.target.checked;
      if (app.screen === "complete") showComplete();
      else renderPickBody();
    },
  });
  multiToggle.checked = multi;
  multiToggle.disabled = boxes.count > 0;
  return [
    packingNotes(order),
    insertChecklist(order),
    required ? h("p", { class: "muted" }, T("shipRequired")) : h("p", { class: "muted" }, T("shipHelp")),
    h("button", { class: ["btn", needPhoto && "btn-primary", size], onclick: takePackPhoto },
      "📷 ", shots ? T("packPhotoMore", { n: shots }) : T("packPhoto")),
    needPhoto ? h("p", { class: "banner banner-warn" }, T("packPhotoRequired")) : null,
    needInserts ? h("p", { class: "banner banner-warn" }, T("insertsMissing")) : null,
    h("label", { class: "row check multi-box" }, multiToggle, T("boxesMore")),
    boxes.count ? h("ul", { class: "box-list" }, ...Array.from({ length: boxes.count }, (_, i) =>
      h("li", null, "✓ ", T("boxLabelled", { n: i + 1, tracking: boxes.list[i] || "" })))) : null,
    h("button", { class: ["btn", required && !blockedLabel ? "btn-primary" : "", size], disabled: blockedLabel, onclick: () => scanOnce(handleLabel) },
      multi ? T("boxScan", { n: boxes.count + 1 }) : T("shipScan")),
    h("button", { class: ["btn", size], disabled: blockedLabel, onclick: typeTracking }, T("shipType")),
    multi && boxes.count ? h("button", { class: ["btn btn-primary", size], onclick: finishBoxes }, T("boxesDone", { n: boxes.count })) : null,
    required || blockedLabel || boxes.count ? null : h("button", { class: ["btn btn-primary", size], onclick: showOrders }, T("orderCompleteNext")),
  ];
}

/** Multi-box: every box has its label; the order has shipped. */
async function finishBoxes() {
  const order = app.order;
  const ev = {
    id: uuid4(),
    kind: "ship",
    order_id: order.id,
    session_id: app.session.id,
    client_scanned_at: new Date().toISOString(),
    client_seq: await store.nextSeq().catch(() => Date.now()),
    final: true,
    offline: !app.status.online,
    local: {},
  };
  try {
    await store.outboxAdd(ev);
  } catch {
    toast(T("storageFailed"), "bad");
    return;
  }
  app.pending.push(ev);
  delete app.multiBox[order.id];
  FX.play("ok");
  toast(T("shipDone"), "ok", 4000);
  if (app.sync) app.sync.kick();
  showOrders();
}

function packShotCount(order) {
  return (order.pack_photos || 0) + (app.packShots[order.id] || 0);
}

/** The packed box, before the label goes on: proof for a "not in the box" claim. */
async function takePackPhoto() {
  const order = app.order;
  if (!order) return;
  const blob = await (STATION ? webcamPhoto() : takePhoto());
  if (!blob) return;
  await savePackPhotos([blob], order.id, S.boxesLabelled(order, pendingFor(order.id)).count + 1);
  FX.play("ok");
  toast(T("packPhotoSaved"), "ok");
  if (app.sync) app.sync.kick();
  if (app.screen === "complete") showComplete();
  else renderPickBody();
}

/** Pack station: a photo from the webcam over the bench, or a file if there's no camera. */
async function webcamPhoto() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return takePhoto();
  let stream = null;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ video: { width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false });
  } catch {
    toast(T("webcamNone"), "warn", 5000);
    return takePhoto();
  }
  const video = h("video", { class: "webcam", playsinline: true, muted: true, autoplay: true });
  video.srcObject = stream;
  try {
    return await dialog(T("packPhoto"), (close) => [
      video,
      h("div", { class: "dialog-actions" },
        h("button", { class: "btn", onclick: () => close(null) }, T("cancel")),
        h("button", { class: "btn", onclick: () => takePhoto().then(close) }, T("webcamChoose")),
        h("button", {
          class: "btn btn-primary btn-lg",
          onclick: () => snapshot(video).then(close, () => toast(T("webcamNone"), "warn")),
        }, "📷 ", T("webcamTake"))),
    ], { wide: true });
  } finally {
    stream.getTracks().forEach((t) => t.stop());
  }
}

async function savePackPhotos(photos, orderId, box = null) {
  for (const blob of photos) {
    try {
      await store.photoAdd({
        id: uuid4(), kind: "pack", order_id: orderId, worker_id: app.session && app.session.workerId, box, blob, created: Date.now(),
      });
      app.packShots[orderId] = (app.packShots[orderId] || 0) + 1;
    } catch {
      toast(T("photoFailed"), "warn");
    }
  }
}

function typeTracking() {
  dialog(T("shipType"), (close) => {
    const input = h("input", {
      class: "input input-xl mono", autocomplete: "off", autocapitalize: "characters", spellcheck: "false",
    });
    return h("form", {
      class: "stack",
      onsubmit: (e) => {
        e.preventDefault();
        close(input.value);
      },
    }, input, h("div", { class: "dialog-actions" },
      h("button", { class: "btn", type: "button", onclick: () => close(null) }, T("cancel")),
      h("button", { class: "btn btn-primary", type: "submit" }, T("manualSubmit"))));
  }).then((v) => v && handleLabel(v));
}

async function handleLabel(rawText) {
  const order = app.order;
  if (!order) return;
  const raw = cleanScan(rawText);
  // An insert's barcode, not a label: tick it off.
  const insert = S.insertFor(order, pendingFor(order.id), raw);
  if (insert) return checkInsert(insert, raw);
  const pendingInserts = (order.inserts || []).filter((i) => !S.insertsDone(order, pendingFor(order.id)).has(i.id));
  if (pendingInserts.length) {
    FX.play("bad");
    const scanRequired = pendingInserts.find((i) => i.scan_required);
    toast(scanRequired && raw.length < 8 ? T("insertWrong", { name: scanRequired.name }) : T("insertsMissing"), "bad", 6000);
    return;
  }
  if (order.require_pack_photo && packShotCount(order) === 0) {
    FX.play("bad");
    toast(T("packPhotoRequired"), "bad", 6000);
    return;
  }
  const check = S.checkLabel(order, raw);
  if (!check.ok) {
    FX.play("bad");
    toast(check.reason === "product" ? T("shipProduct") : T("shipTooShort"), "bad", 6000);
    return;
  }
  const ev = {
    id: uuid4(),
    kind: "ship",
    order_id: order.id,
    session_id: app.session.id,
    client_scanned_at: new Date().toISOString(),
    client_seq: await store.nextSeq().catch(() => Date.now()),
    tracking_number: String(raw).trim().slice(0, 200),
    final: !app.multiBox[order.id],
    offline: !app.status.online,
    local: {},
  };
  const boxes = S.boxesLabelled(order, pendingFor(order.id));
  if (boxes.list.includes(check.tracking)) {
    FX.play("bad");
    toast(T("syncRefused", { message: "That label is already on this order." }), "bad", 6000);
    return;
  }
  try {
    await store.outboxAdd(ev);
  } catch {
    toast(T("storageFailed"), "bad");
    return;
  }
  app.pending.push(ev);
  FX.play("ok");
  if (app.sync) app.sync.kick();
  if (!ev.final) {
    toast(T("boxAdded", { n: boxes.count + 1 }), "ok", 5000);
    if (app.screen === "complete") showComplete();
    else renderPickBody();
    return;
  }
  toast(`${T("shipDone")} · ${T("shipDoneDetail", { tracking: check.tracking })}`, "ok", 5000);
  showOrders();
}

function typeBarcode() {
  dialog(T("manualTitle"), (close) => {
    const input = h("input", {
      class: "input input-xl mono", autocomplete: "off", autocapitalize: "characters", spellcheck: "false",
      placeholder: T("manualPlaceholder"), inputmode: "text",
    });
    return h("form", {
      class: "stack",
      onsubmit: (e) => {
        e.preventDefault();
        close(input.value);
      },
    }, input, h("div", { class: "dialog-actions" },
      h("button", { class: "btn", type: "button", onclick: () => close(null) }, T("cancel")),
      h("button", { class: "btn btn-primary", type: "submit" }, T("manualSubmit"))));
  }).then((v) => v && handleScan(v));
}

async function undoLast() {
  if (app.batch) {
    // The most recent pick in any of the batch's orders.
    const hits = app.batch.orders.map((o) => S.lastUndoable(app.history, o.id)).filter(Boolean);
    hits.sort((a, b) => app.history.indexOf(b) - app.history.indexOf(a));
    if (hits[0]) app.order = batchOrder(hits[0].orderId);
  }
  const lastScan = S.lastUndoable(app.history, app.order.id);
  if (!lastScan) {
    toast(T("pickNothingToUndo"));
    return;
  }
  const line = app.order.lines.find((l) => l.id === lastScan.lineId);
  const ok = await dialog(T("pickUndo"), (close) => [
    h("p", null, T("pickUndoConfirm", { item: line ? lineLabel(line) : "" })),
    h("div", { class: "dialog-actions" },
      h("button", { class: "btn", onclick: () => close(false) }, T("cancel")),
      h("button", { class: "btn btn-primary", onclick: () => close(true) }, T("pickUndo"))),
  ]);
  if (!ok) return;
  const ev = {
    id: uuid4(),
    kind: "void",
    order_id: app.order.id,
    session_id: app.session.id,
    client_scanned_at: new Date().toISOString(),
    client_seq: await store.nextSeq().catch(() => Date.now()),
    target_scan_id: lastScan.id,
    offline: !app.status.online,
    local: { lineId: lastScan.lineId, qty: lastScan.qty || 1 },
  };
  try {
    await store.outboxAdd(ev);
  } catch {
    toast(T("storageFailed"), "bad");
    return;
  }
  app.pending.push(ev);
  remember({ id: ev.id, orderId: app.order.id, kind: "void", target: lastScan.id });
  app.targetLineId = lastScan.lineId;
  toast(T("pickUndone"), "ok");
  renderPickBody();
  if (app.sync) app.sync.kick();
}

async function flagProblem(scanId) {
  const lines = currentLines();
  const target = S.nextLine(lines, app.targetLineId);
  const photos = [];
  const result = await dialog(T("flagTitle"), (close) => {
    const which = h("select", { class: "input" },
      h("option", { value: "" }, T("flagWholeOrder")),
      ...S.sortForWalking(lines).map((l) =>
        h("option", { value: l.id, selected: target && l.id === target.id }, lineLabel(l))));
    const note = h("input", { class: "input", placeholder: T("flagNote"), maxlength: "500" });
    return h("div", { class: "stack" },
      h("label", null, T("flagWhich")), which,
      photoPicker(photos),
      h("div", { class: "reason-grid" }, ...FLAG_REASONS.map((reason) =>
        h("button", {
          class: "btn btn-lg",
          onclick: () => close({ reason, line: which.value || null, note: note.value.trim() || null }),
        }, T(`flagReason_${reason}`)))),
      note,
      h("div", { class: "dialog-actions" }, h("button", { class: "btn", onclick: () => close(null) }, T("cancel"))));
  });
  if (!result) return;
  const ev = {
    id: uuid4(),
    kind: "flag",
    order_id: app.order.id,
    session_id: app.session.id,
    client_scanned_at: new Date().toISOString(),
    client_seq: await store.nextSeq().catch(() => Date.now()),
    line_item_id: result.line,
    scan_event_id: scanId || null,
    reason: result.reason,
    note: result.note,
    offline: !app.status.online,
    local: {},
  };
  try {
    await store.outboxAdd(ev);
  } catch {
    toast(T("storageFailed"), "bad");
    return;
  }
  app.pending.push(ev);
  await savePhotos(photos, ev.id);
  app.order = { ...app.order, open_flags: (app.order.open_flags || 0) + 1 };
  toast(T("flagSent"), "ok");
  renderPickBody();
  if (app.sync) app.sync.kick();
}

/** "Add photo" button with thumbnails; fills `photos` with Blobs. */
function photoPicker(photos) {
  const thumbs = h("div", { class: "photo-thumbs" });
  const label = h("span", null, T("photoAdd"));
  const btn = h("button", {
    class: "btn btn-lg photo-btn",
    type: "button",
    onclick: async () => {
      if (photos.length >= MAX_PHOTOS) return;
      const blob = await takePhoto();
      if (!blob) return;
      photos.push(blob);
      const url = URL.createObjectURL(blob);
      thumbs.appendChild(h("img", { src: url, alt: "", class: "photo-thumb" }));
      label.textContent = photos.length >= MAX_PHOTOS ? T("photoCount", { n: photos.length }) : T("photoMore");
      if (photos.length >= MAX_PHOTOS) btn.disabled = true;
    },
  }, "📷 ", label);
  return h("div", { class: "photo-picker" }, btn, thumbs);
}

async function savePhotos(photos, flagId) {
  for (const blob of photos) {
    try {
      await store.photoAdd({ id: uuid4(), flag_id: flagId, blob, created: Date.now() });
    } catch {
      toast(T("photoFailed"), "warn");
    }
  }
}

// ---------------------------------------------------------------------------
// Short picks: "found only 2 of 3"
// ---------------------------------------------------------------------------

async function shortPick() {
  const lines = currentLines();
  const open = S.sortForWalking(lines).filter((l) => S.remaining(l) > 0);
  if (!open.length) {
    toast(T("shortNothing"));
    return;
  }
  const target = S.nextLine(lines, app.targetLineId) || open[0];
  const photos = [];
  const result = await dialog(T("shortTitle"), (close) => {
    let lineId = target.id;
    let qty = S.remaining(target);
    let reason = null;
    const lineOf = () => open.find((l) => l.id === lineId);
    const qtyLabel = h("div", { class: "qty-value" });
    const qtyHint = h("div", { class: "muted small" });
    const paintQty = () => {
      const left = S.remaining(lineOf());
      qty = Math.min(Math.max(1, qty), left);
      qtyLabel.textContent = String(qty);
      qtyHint.textContent = T("shortOf", { n: qty, left });
    };
    const which = h("select", {
      class: "input",
      onchange: () => {
        lineId = which.value;
        qty = S.remaining(lineOf());
        paintQty();
      },
    }, ...open.map((l) => h("option", { value: l.id, selected: l.id === lineId },
      `${lineLabel(l)} (${l.scanned_quantity}/${l.expected_quantity})`)));
    const submit = h("button", { class: "btn btn-primary btn-lg", disabled: true, onclick: () => {
      close({ lineId, qty, reason, note: note.value.trim() || null });
    } }, T("shortSubmit"));
    const reasonBtns = SHORT_REASONS.map((r) => h("button", {
      class: "btn btn-lg",
      type: "button",
      "aria-pressed": "false",
      onclick: (e) => {
        reason = r;
        for (const b of reasonBtns) b.setAttribute("aria-pressed", String(b === e.currentTarget));
        submit.disabled = false;
      },
    }, T(`shortReason_${r}`)));
    const note = h("input", { class: "input", placeholder: T("flagNote"), maxlength: "500" });
    paintQty();
    return h("div", { class: "stack" },
      h("label", null, T("shortWhich")), which,
      h("label", null, T("shortHowMany")),
      h("div", { class: "qty-stepper" },
        h("button", { class: "btn btn-lg", type: "button", "aria-label": "−", onclick: () => {
          qty -= 1;
          paintQty();
        } }, "−"),
        qtyLabel,
        h("button", { class: "btn btn-lg", type: "button", "aria-label": "+", onclick: () => {
          qty += 1;
          paintQty();
        } }, "+")),
      qtyHint,
      h("label", null, T("shortWhy")),
      h("div", { class: "reason-grid" }, ...reasonBtns),
      photoPicker(photos),
      note,
      h("div", { class: "dialog-actions" },
        h("button", { class: "btn", type: "button", onclick: () => close(null) }, T("cancel")),
        submit));
  });
  if (!result) return;
  const ev = {
    id: uuid4(),
    kind: "short",
    order_id: app.order.id,
    session_id: app.session.id,
    client_scanned_at: new Date().toISOString(),
    client_seq: await store.nextSeq().catch(() => Date.now()),
    line_item_id: result.lineId,
    quantity: result.qty,
    short_reason: result.reason,
    note: result.note,
    offline: !app.status.online,
    local: { lineId: result.lineId },
  };
  try {
    await store.outboxAdd(ev);
  } catch {
    toast(T("storageFailed"), "bad");
    return;
  }
  app.pending.push(ev);
  await savePhotos(photos, ev.id);
  app.order = { ...app.order, open_flags: (app.order.open_flags || 0) + 1 };
  app.targetLineId = null;
  toast(T("shortSent", { n: result.qty }), "warn", 5000);
  if (app.sync) app.sync.kick();
  if (S.progress(currentLines()).complete) showComplete();
  else renderPickBody();
}

// ---------------------------------------------------------------------------
// Empty bins and restocking
// ---------------------------------------------------------------------------

async function reportEmptyBin(line) {
  if (!line) return;
  if (app.batch && line.orderId) app.order = batchOrder(line.orderId);
  const item = [line.location, lineLabel(line)].filter(Boolean).join(" · ");
  const ok = await dialog(T("pickBinEmpty"), (close) => [
    h("p", null, T("binEmptyConfirm", { item })),
    h("div", { class: "dialog-actions" },
      h("button", { class: "btn", onclick: () => close(false) }, T("cancel")),
      h("button", { class: "btn btn-primary", onclick: () => close(true) }, T("binEmptyReport"))),
  ]);
  if (!ok) return;
  const ev = {
    id: uuid4(),
    kind: "restock",
    order_id: app.order.id,
    session_id: app.session.id,
    client_scanned_at: new Date().toISOString(),
    client_seq: await store.nextSeq().catch(() => Date.now()),
    line_item_id: line.id,
    offline: !app.status.online,
    local: {},
  };
  try {
    await store.outboxAdd(ev);
  } catch {
    toast(T("storageFailed"), "bad");
    return;
  }
  app.pending.push(ev);
  toast(T("binEmptySent"), "ok", 4000);
  if (app.sync) app.sync.kick();
}

async function showRestock() {
  if (!app.session) return showPin();
  stopCamera();
  const listEl = h("div", { class: "order-list" }, h("div", { class: "skeleton" }));
  setScreen("restock",
    topbar(),
    h("main", { class: "screen" },
      h("div", { class: "pick-head" },
        h("button", { class: "btn btn-sm", onclick: showOrders }, "← ", T("back")),
        h("h1", null, T("restockTitle"))),
      listEl));
  let tasks;
  try {
    tasks = (await api("/api/worker/restock")).tasks;
  } catch (e) {
    mount(listEl, h("p", { class: "banner banner-warn" }, e.isNetwork ? T("restockNeedsNetwork") : e.message));
    return;
  }
  if (app.screen !== "restock") return;
  if (!tasks.length) {
    mount(listEl, h("p", { class: "empty" }, T("restockEmpty")));
    return;
  }
  mount(listEl, ...tasks.map((t) => h("div", { class: "order-row restock-row" },
    h("div", { class: "order-row-main" },
      h("span", { class: "target-location restock-loc" }, t.location || "–"),
      h("span", { class: "order-number" }, t.description || t.sku || t.barcode)),
    h("div", { class: "order-row-sub" },
      h("span", { class: "mono" }, t.barcode || ""),
      t.reported_by ? h("span", null, T("restockReportedBy", { name: t.reported_by })) : null),
    h("button", {
      class: "btn btn-primary btn-lg",
      onclick: async (e) => {
        e.currentTarget.disabled = true;
        try {
          await api(`/api/worker/restock/${t.id}/done`, { method: "POST" });
          FX.play("ok");
          toast(T("restockMarked"), "ok");
          showRestock();
        } catch (err) {
          toast(err.isNetwork ? T("restockNeedsNetwork") : err.message, "bad");
          e.currentTarget.disabled = false;
        }
      },
    }, "✓ ", T("restockDone")))));
}

// ---------------------------------------------------------------------------
// Time clock
// ---------------------------------------------------------------------------

/** After the PIN (and the privacy notice): clock in first, if the warehouse uses the time clock. */
async function afterLogin() {
  try {
    const r = await api("/api/worker/shift");
    app.timeClock = Boolean(r.enabled);
    app.shift = r.shift;
    if (r.enabled && !r.shift) return showClockIn();
  } catch {
    /* offline: straight to work */
  }
  showOrders();
}

function showClockIn() {
  setScreen("clock",
    topbar(),
    h("main", { class: "screen narrow center" },
      h("div", { class: "complete-check clock-icon" }, "⏱"),
      h("h1", null, T("clockTitle")),
      h("p", { class: "muted" }, T("clockHelp")),
      h("div", { class: "stack" },
        h("button", {
          class: "btn btn-primary btn-xl",
          onclick: async () => {
            try {
              const r = await api("/api/worker/clock-in", { method: "POST" });
              app.shift = r.shift;
              FX.play("ok");
              toast(T("clockedIn", { time: fmtTime(r.shift.clock_in) }), "ok");
            } catch (e) {
              toast(e.isNetwork ? T("pinNeedsNetwork") : e.message, "bad");
              return;
            }
            showOrders();
          },
        }, T("clockIn")),
        h("button", { class: "btn btn-lg", onclick: showOrders }, T("clockSkip")))));
}

// ---------------------------------------------------------------------------
// Scanner and sound settings (per phone)
// ---------------------------------------------------------------------------

/** The big scan control: the camera, or a "scanner ready" sign when a hardware scanner is set up. */
function scanButton() {
  if (!usesHardwareScanner(app.prefs)) {
    return h("button", { class: "btn btn-primary btn-xl action-scan", onclick: startCamera }, T("pickCamera"));
  }
  return h("div", { class: "scanner-ready action-scan" },
    h("span", { class: "scanner-dot" }), T("scannerReady"),
    h("button", { class: "btn btn-sm", "aria-label": T("pickCamera"), onclick: startCamera }, "📷"));
}

function showSettings() {
  const draft = { ...PREF_DEFAULTS, ...app.prefs };
  dialog(T("settingsTitle"), (close) => {
    const types = [["camera", "scannerCamera"], ["wedge", "scannerWedge"], ["ring", "scannerRing"], ["rugged", "scannerRugged"]];
    const got = h("p", { class: "muted small scanner-got" }, T("scannerTest"));
    const test = h("input", {
      class: "input mono", autocomplete: "off", autocapitalize: "off", spellcheck: "false", placeholder: T("scannerTest"),
      onkeydown: (e) => {
        if (e.key !== "Enter" && e.key !== "Tab") return;
        e.preventDefault();
        got.textContent = T("scannerGot", { code: cleanScan(test.value) || "–" });
        test.value = "";
        FX.play("ok");
      },
    });
    const check = (key, label) => {
      const box = h("input", { type: "checkbox", onchange: (e) => { draft[key] = e.target.checked; FX.configure(draft); } });
      box.checked = Boolean(draft[key]);
      return h("label", { class: "row check" }, box, T(label));
    };
    return h("div", { class: "stack settings" },
      h("label", null, T("scannerType")),
      h("div", { class: "scanner-types" }, ...types.map(([value, label]) => {
        const radio = h("input", { type: "radio", name: "scanner", value, onchange: () => { draft.scanner = value; } });
        radio.checked = draft.scanner === value;
        return h("label", { class: "row check" }, radio, T(label));
      })),
      h("p", { class: "muted small" }, T("scannerHelp")),
      test, got,
      check("sound", "soundOn"), check("loud", "soundLoud"), check("vibrate", "vibrateOn"), check("strongVibrate", "vibrateStrong"),
      h("div", { class: "grid-2" },
        h("button", { class: "btn", type: "button", onclick: () => { FX.unlockAudio(); FX.play("ok"); } }, T("testRight")),
        h("button", { class: "btn", type: "button", onclick: () => { FX.unlockAudio(); FX.play("bad"); } }, T("testWrong"))),
      h("div", { class: "dialog-actions" },
        h("button", { class: "btn", type: "button", onclick: () => close(null) }, T("cancel")),
        h("button", { class: "btn btn-primary", type: "button", onclick: () => close(draft) }, T("settingsSave"))));
  }).then((saved) => {
    app.prefs = saved ? savePrefs(saved) : app.prefs;
    FX.configure(app.prefs);
    if (saved) toast(T("settingsSaved"), "ok");
  });
}

// ---------------------------------------------------------------------------
// Camera
// ---------------------------------------------------------------------------

async function startCamera() {
  FX.unlockAudio();
  if (app.camera) {
    bumpCameraIdle();
    return;
  }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    toast(T("cameraUnavailable"), "warn", 6000);
    return;
  }
  app.camera = { scanner: null, idleUntil: 0, tick: null };
  attachCamera();
}

function attachCamera() {
  const panel = document.getElementById("camera");
  const video = document.getElementById("video");
  const canvas = document.getElementById("canvas");
  if (!panel || !video) return;
  panel.hidden = false;
  const cam = app.camera;
  if (cam.scanner) cam.scanner.stop();
  cam.scanner = new Scanner(video, canvas, (text) => {
    bumpCameraIdle();
    handleScan(text);
  });
  cam.scanner.start().then(() => {
    const torch = document.getElementById("torch");
    if (torch && cam.scanner.hasTorch()) torch.hidden = false;
  }).catch((e) => {
    if (app.camera !== cam) return;
    stopCamera();
    const denied = e && (e.name === "NotAllowedError" || e.name === "SecurityError");
    toast(denied ? T("cameraDenied") : T("cameraUnavailable"), "warn", 6000);
  });
  if (!cam.idleUntil) bumpCameraIdle();
  clearInterval(cam.tick);
  cam.tick = setInterval(() => {
    const left = Math.max(0, Math.ceil((cam.idleUntil - Date.now()) / 1000));
    const label = document.getElementById("camera-countdown");
    if (label) label.textContent = T("pickCameraOff", { s: left });
    if (left <= 0 && !app.overlay) stopCamera();
  }, 500);
}

function bumpCameraIdle() {
  if (app.camera) app.camera.idleUntil = Date.now() + CAMERA_IDLE_MS;
}

function toggleTorch() {
  if (app.camera && app.camera.scanner) app.camera.scanner.toggleTorch();
}

function stopCamera() {
  const cam = app.camera;
  if (!cam) return;
  app.camera = null;
  clearInterval(cam.tick);
  if (cam.scanner) cam.scanner.stop();
  const panel = document.getElementById("camera");
  if (panel) panel.hidden = true;
}

/** One-shot camera scan in a dialog (setup QR, pick-sheet QR). */
function scanOnce(onText) {
  FX.unlockAudio();
  let scanner = null;
  dialog(T("pickCamera"), (close) => {
    const video = h("video", { playsinline: true, muted: true, autoplay: true, class: "dialog-video" });
    const canvas = h("canvas", { hidden: true });
    scanner = new Scanner(video, canvas, (text) => {
      FX.play("ok");
      close(text);
    });
    scanner.start().catch(() => {
      toast(T("cameraUnavailable"), "warn");
      close(null);
    });
    return h("div", { class: "stack" }, h("div", { class: "camera camera-dialog" }, video, h("div", { class: "camera-frame" })),
      h("button", { class: "btn", onclick: () => close(null) }, T("cancel")));
  }).then((text) => {
    if (scanner) scanner.stop();
    if (text) onText(text);
  });
}

// ---------------------------------------------------------------------------
// Hardware (Bluetooth / USB "keyboard wedge") scanners
// ---------------------------------------------------------------------------
// A wedge scanner types the barcode as fast keystrokes ending in Enter. When
// no text field has focus, collect those keystrokes and treat them as a scan.

function onKeydown(e) {
  const target = e.target;
  const typing = target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT");
  if (typing || document.querySelector("dialog[open]")) return;
  if (app.screen === "pin" && app.pinKeyHandler) {
    app.pinKeyHandler(e);
    return;
  }
  const now = Date.now();
  const gap = keyGapMs(app.prefs);
  if (e.key === "Enter" || e.key === "Tab") {
    clearTimeout(app.wedgeTimer);
    const code = app.wedgeBuffer;
    app.wedgeBuffer = "";
    if (code.length >= 3) {
      e.preventDefault();
      routeWedgeScan(code);
    }
    return;
  }
  if (e.key.length !== 1) return;
  if (now - app.wedgeAt > gap) app.wedgeBuffer = "";
  app.wedgeBuffer += e.key;
  app.wedgeAt = now;
  // Some scanners are set up with no Enter at the end: a fast burst of 6+
  // keys followed by silence is a scan too.
  clearTimeout(app.wedgeTimer);
  if (usesHardwareScanner(app.prefs)) {
    app.wedgeTimer = setTimeout(() => {
      const code = app.wedgeBuffer;
      if (code.length >= 6 && Date.now() - app.wedgeAt >= gap) {
        app.wedgeBuffer = "";
        routeWedgeScan(code);
      }
    }, gap + 80);
  }
}

function routeWedgeScan(code) {
  FX.unlockAudio();
  if (app.screen === "pick") handleScan(code);
  else if (app.screen === "complete") handleLabel(code);
  else if (app.screen === "orders") openFromCode(cleanScan(code));
}

// ---------------------------------------------------------------------------
// Sync results
// ---------------------------------------------------------------------------

async function onSyncResult(batch, resp) {
  app.pending = await store.outboxAll().catch(() => app.pending);
  const touched = new Set(Object.keys(resp.orders || {}));
  const needRefetch = [];
  for (const id of touched) {
    const cached = await store.getOrder(id).catch(() => null);
    if (!cached) continue;
    const { order, needsRefetch } = S.applyServerState(cached, resp.orders[id]);
    await store.putOrder(order);
    if (app.batch) {
      const i = app.batch.orders.findIndex((o) => o.id === id);
      if (i >= 0) app.batch.orders[i] = { ...order, tote: app.batch.orders[i].tote };
    }
    if (app.order && app.order.id === id) {
      const linesById = new Map(order.lines.map((l) => [l.id, l]));
      for (const c of S.corrections(batch.filter((e) => e.order_id === id), resp.events, linesById)) {
        toast(T(c.key, c.vars), c.kind, 9000);
        if (c.kind !== "ok") FX.play(c.kind);
      }
      app.order = order;
    }
    if (needsRefetch) needRefetch.push(id);
  }
  const kinds = new Map(batch.map((e) => [e.id, e.kind]));
  for (const o of resp.events) {
    if (o.status !== "error") continue;
    if (o.error && o.error.code === "order_cancelled") {
      toast(T("orderCancelled"), "bad", 9000);
      FX.play("bad");
    } else if (o.error && ["ship", "short", "flag", "finish"].includes(kinds.get(o.id))) {
      // A label or report the server refused (label already used, order not
      // finished...): the worker has to know it didn't count.
      toast(T("syncRefused", { message: o.error.message }), "bad", 10000);
      FX.play("bad");
    }
  }
  if (resp.access && !resp.access.allowed) app.locked = resp.access.message;
  await countPackShots();
  for (const id of needRefetch) {
    try {
      const fresh = await fetchOrder(id);
      if (app.order && app.order.id === id) app.order = fresh;
      if (app.batch) {
        const i = app.batch.orders.findIndex((o) => o.id === id);
        if (i >= 0) app.batch.orders[i] = fresh;
      }
    } catch {
      break;
    }
  }
  if (app.screen === "pick" && app.order && !app.overlay && !document.querySelector("dialog[open]")) {
    renderPickBody();
  } else if (app.screen === "complete" && app.order && touched.has(app.order.id)) {
    showComplete();
  }
}

function startSync() {
  if (app.sync) app.sync.stop();
  app.sync = new Sync({
    deviceToken: () => app.device && app.device.token,
    onResult: onSyncResult,
    onStatus: (s) => {
      app.status = s;
      refreshChip();
    },
    onAuthLost: unlink,
    onPhotoRejected: (e) => toast(T("photoRejected", { message: e.message }), "warn", 8000),
  });
  app.sync.start();
}

// ---------------------------------------------------------------------------
// End of shift, and dead ends
// ---------------------------------------------------------------------------

async function endShift() {
  stopCamera();
  let clockOut = false;
  if (app.shift) {
    const choice = await dialog(T("clockOutAsk"), (close) => [
      h("p", { class: "muted" }, T("clockOutHelp")),
      h("div", { class: "stack" },
        h("button", { class: "btn btn-primary btn-lg", onclick: () => close("out") }, T("clockOutYes")),
        h("button", { class: "btn btn-lg", onclick: () => close("stay") }, T("clockOutNo")),
        h("button", { class: "btn", onclick: () => close(null) }, T("cancel"))),
    ]);
    if (!choice) return;
    clockOut = choice === "out";
  }
  const left = app.sync ? await app.sync.flush() : await store.outboxCount().catch(() => 0);
  let summary = null;
  try {
    summary = await api("/api/worker/summary");
    if (clockOut) await api("/api/worker/clock-out", { method: "POST" });
    await api("/api/worker/logout", { method: "POST" });
  } catch {
    summary = null;
  }
  const name = app.session ? app.session.workerName : "";
  app.shift = null;
  endSessionLocally();
  const stat = (n, label) => h("div", { class: "stat" }, h("div", { class: "stat-n" }, String(n)), h("div", { class: "stat-l" }, label));
  setScreen("summary",
    topbar(),
    h("main", { class: "screen narrow center" },
      h("h1", null, T("summaryTitle")),
      h("p", { class: "muted" }, name),
      summary
        ? h("div", { class: "stats" },
          stat(summary.units_picked, T("summaryPicked")),
          stat(summary.errors_caught, T("summaryCaught")),
          stat(summary.needs_review, T("summaryReview")),
          stat(summary.orders_completed, T("summaryOrders")),
          summary.clock_hours != null ? stat(summary.clock_hours.toFixed(1), T("summaryHours")) : null,
          summary.uph != null ? stat(summary.uph, T("summaryUph")) : null)
        : h("p", { class: "banner banner-warn" }, T("summaryOffline")),
      left > 0 ? h("p", { class: "banner banner-warn" }, T("summaryUnsynced", { n: left })) : null,
      h("button", { class: "btn btn-primary btn-xl", onclick: () => showPin() }, T("summaryDone"))));
}

function showLocked() {
  stopCamera();
  setScreen("locked",
    topbar(),
    h("main", { class: "screen narrow center" },
      h("h1", null, T("lockedTitle")),
      h("p", { class: "banner banner-bad" }, app.locked || ""),
      h("p", { class: "muted" }, T("lockedHelp")),
      h("button", {
        class: "btn btn-xl",
        onclick: () => {
          app.locked = null;
          if (app.session) showOrders();
          else showPin();
        },
      }, T("ordersRefresh"))));
}

function showUnlinked() {
  setScreen("unlinked",
    topbar(),
    h("main", { class: "screen narrow center" },
      h("h1", null, T("unlinkedTitle")),
      h("p", { class: "muted" }, T("unlinkedHelp")),
      h("button", { class: "btn btn-primary btn-xl", onclick: () => showLink() }, T("linkButton"))));
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

async function boot() {
  const saved = readJson(LS_LANG);
  // First run: the phone's own language, if we have it.
  const phoneLang = (navigator.language || "").toLowerCase().slice(0, 2);
  setLang(saved || (LANGUAGES.some(([code]) => code === phoneLang) ? phoneLang : "en"));
  FX.configure(app.prefs);
  if (STATION) document.title = "Autorack Pack Station";
  document.addEventListener("keydown", onKeydown);
  requestPersistence();
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/w/sw.js", { scope: "/w/" }).catch(() => {});
  }
  app.history = (await store.metaGet("history").catch(() => null)) || [];
  app.pending = await store.outboxAll().catch(() => []);

  const linkCode = new URLSearchParams(location.search).get("link");
  if (!app.device) {
    showLink(linkCode || "");
    return;
  }
  if (linkCode) history.replaceState(null, "", location.pathname);
  startSync();
  if (app.session) showOrders();
  else showPin();
}

boot();
