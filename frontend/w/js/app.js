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
import { T, getLang, setLang } from "./i18n.js";
import { Scanner } from "./scanner.js";
import * as S from "./state.js";
import { requestPersistence, store } from "./store.js";
import { Sync } from "./sync.js";

const LS_DEVICE = "ar.device";
const LS_SESSION = "ar.session";
const LS_LANG = "ar.lang";
const CAMERA_IDLE_MS = 20000;
const MATCH_OVERLAY_MS = 900;
const PREFETCH_LIMIT = 40;
const FLAG_REASONS = ["wrong_item_in_location", "out_of_stock", "damaged", "label_unreadable", "other"];

const root = document.getElementById("app");

const app = {
  device: readJson(LS_DEVICE), // {token, warehouseName, label}
  session: readJson(LS_SESSION), // {token, id, workerId, workerName, expiresAt}
  screen: null,
  order: null, // cached payload of the order being picked
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

function langToggle() {
  return h("button", {
    class: "link-btn",
    onclick: () => {
      writeJson(LS_LANG, setLang(getLang() === "en" ? "es" : "en"));
      rerender();
    },
  }, T("language"));
}

function topbar(...right) {
  return h("header", { class: "topbar" },
    h("div", { class: "topbar-left" },
      brandLockup(),
      app.device ? h("span", { class: "topbar-title" }, app.device.warehouseName) : null),
    h("div", { class: "topbar-right" }, statusChip(), ...right));
}

function rerender() {
  const screens = { link: showLink, pin: showPin, orders: showOrders, pick: showPick, locked: showLocked };
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
        body: { join_code: code.value, label: label.value || "Phone" },
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
      showOrders();
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
// Orders
// ---------------------------------------------------------------------------

async function cachedOrderList() {
  return (await store.metaGet("orderList")) || [];
}

async function showOrders() {
  if (!app.session) return showPin();
  stopCamera();
  app.order = null;
  const listEl = h("div", { class: "order-list" }, h("div", { class: "skeleton" }));
  const note = h("p", { class: "muted small" });
  setScreen("orders",
    topbar(),
    h("main", { class: "screen" },
      h("div", { class: "row-between" },
        h("h1", null, T("ordersGreeting", { name: app.session.workerName })),
        h("div", { class: "row" }, langToggle(), h("button", { class: "btn btn-sm", onclick: endShift }, T("endShift")))),
      h("div", { class: "grid-2" },
        h("button", { class: "btn btn-primary btn-xl", onclick: () => scanOnce(openFromCode) }, T("ordersScanSheet")),
        h("button", { class: "btn btn-xl", onclick: typeOrderNumber }, T("ordersTypeNumber"))),
      note,
      listEl));

  let orders;
  let offline = false;
  try {
    const r = await api("/api/worker/orders");
    orders = r.orders;
    await store.metaSet("orderList", orders);
  } catch (e) {
    if (!e.isNetwork) return;
    offline = true;
    orders = await cachedOrderList();
  }
  if (app.screen !== "orders") return;
  note.textContent = offline ? T("ordersOfflineList") : "";
  const cached = new Map((await store.allOrders()).map((o) => [o.id, o]));
  renderOrderList(listEl, orders, cached);
  if (!offline) prefetch(orders, cached).then(async () => {
    if (app.screen !== "orders") return;
    renderOrderList(listEl, orders, new Map((await store.allOrders()).map((o) => [o.id, o])));
  });
}

function renderOrderList(listEl, orders, cached) {
  if (!orders.length) {
    mount(listEl, h("p", { class: "empty" }, T("ordersEmpty")));
    return;
  }
  mount(listEl, ...orders.map((o) => {
    const c = cached.get(o.id);
    const ready = c && c.version >= o.version;
    const pct = o.units_expected ? Math.round((100 * o.units_scanned) / o.units_expected) : 0;
    return h("button", { class: "order-row", onclick: () => openOrder(o.id) },
      h("div", { class: "order-row-main" },
        h("span", { class: "order-number" }, o.external_order_number || o.id.slice(0, 8)),
        h("span", { class: `badge badge-${o.status}` }, T(`status_${o.status}`)),
        o.assigned_to_me ? h("span", { class: "badge badge-assigned" }, T("ordersAssigned")) : null),
      h("div", { class: "order-row-sub" },
        h("span", null, T("progressUnits", { done: o.units_scanned, total: o.units_expected })),
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
  app.order = order;
  app.targetLineId = null;
  app.pending = await store.outboxAll().catch(() => []);
  showPick();
  if (navigator.onLine) refreshOrderInBackground(order.id);
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
    app.locked ? h("div", { class: "banner banner-bad" }, app.locked) : null);

  const targetCard = target
    ? h("section", { class: "target" },
      h("div", { class: "target-label" }, T("pickNext")),
      target.location ? h("div", { class: "target-location" }, target.location) : null,
      h("div", { class: "target-name" }, lineLabel(target)),
      h("div", { class: "target-meta" },
        target.sku ? h("span", { class: "mono" }, target.sku) : null,
        h("span", { class: "mono muted" }, target.expected_barcode)),
      h("div", { class: "target-qty" },
        T("pickQty", { done: target.scanned_quantity, total: target.expected_quantity })))
    : h("section", { class: "target target-done" }, h("div", { class: "target-name" }, T("orderComplete")));

  mount(document.getElementById("pick-body"),
    targetCard,
    h("div", { class: "actions" },
      h("button", { class: "btn btn-primary btn-xl action-scan", onclick: startCamera }, T("pickCamera")),
      h("button", { class: "btn btn-lg", onclick: typeBarcode }, T("pickType")),
      h("button", { class: "btn btn-lg", onclick: () => flagProblem(null) }, T("pickFlag")),
      h("button", { class: "btn btn-lg", onclick: undoLast }, T("pickUndo"))),
    h("h2", { class: "section-title" }, T("pickAllLines")),
    h("ul", { class: "lines" }, ...S.sortForWalking(lines).map((l) => {
      const done = l.scanned_quantity >= l.expected_quantity;
      return h("li", {
        class: ["line", done && "line-done", target && l.id === target.id && "line-target"],
        onclick: () => {
          if (done) return;
          app.targetLineId = l.id;
          renderPickBody();
        },
      },
      h("div", { class: "line-main" },
        h("span", { class: "line-name" }, lineLabel(l)),
        h("span", { class: "line-qty" }, done ? `✓ ${l.expected_quantity}` : `${l.scanned_quantity}/${l.expected_quantity}`)),
      h("div", { class: "line-sub" },
        l.location ? h("span", null, l.location) : null,
        h("span", { class: "mono" }, l.expected_barcode)));
    })));
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
  const raw = String(rawText);
  if (!raw.trim()) return;
  if (S.isOrderCode(raw)) {
    showResult({ result: "order_code" });
    return;
  }
  const order = app.order;
  const lines = currentLines();
  const target = S.nextLine(lines, app.targetLineId);
  const c = S.classify(order, lines, raw);
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
    local: { result: c.result, lineId: c.lineId },
  };
  try {
    await store.outboxAdd(ev);
  } catch {
    showResult({ result: "storage_failed" });
    return;
  }
  app.pending.push(ev);
  remember({ id: ev.id, orderId: order.id, kind: "scan", result: c.result, lineId: c.lineId, at: ev.client_scanned_at });
  const after = currentLines();
  const line = after.find((l) => l.id === c.lineId);
  if (c.result === "match" && line && line.scanned_quantity >= line.expected_quantity) app.targetLineId = null;
  showResult({ ...c, line, scanId: ev.id, complete: S.progress(after).complete });
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
  const spec = {
    match: ["ok", "✓", T("resultMatch"), r.line ? `${lineLabel(r.line)} · ${T("resultMatchDetail", { done: r.line.scanned_quantity, total: r.line.expected_quantity })}` : ""],
    mismatch: ["bad", "✕", T("resultMismatch"), T("resultMismatchDetail")],
    over_pick: ["warn", "!", T("resultOverPick"), r.line ? `${lineLabel(r.line)} · ${T("resultOverPickDetail")}` : T("resultOverPickDetail")],
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
      r.result === "mismatch" || r.result === "review"
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
    class: ["overlay", `overlay-${kind}`], role: "alertdialog", "aria-live": "assertive",
    onclick: kind === "ok" ? dismiss : null,
  },
  h("div", { class: "overlay-icon" }, icon),
  h("div", { class: "overlay-title" }, title),
  detail ? h("div", { class: "overlay-detail" }, detail) : null,
  buttons);
  document.body.appendChild(el);
  app.overlay = { el, timer: kind === "ok" ? setTimeout(dismiss, MATCH_OVERLAY_MS) : null, dismiss, close };
}

function showComplete() {
  stopCamera();
  FX.play("ok");
  setScreen("complete",
    topbar(),
    h("main", { class: "screen narrow center complete" },
      h("div", { class: "complete-check" }, "✓"),
      h("h1", null, T("orderComplete")),
      h("p", { class: "muted" }, T("orderCompleteDetail", { order: app.order.external_order_number || "" })),
      h("button", { class: "btn btn-primary btn-xl", onclick: showOrders }, T("orderCompleteNext"))));
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
    local: { lineId: lastScan.lineId },
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
  const result = await dialog(T("flagTitle"), (close) => {
    const which = h("select", { class: "input" },
      h("option", { value: "" }, T("flagWholeOrder")),
      ...S.sortForWalking(lines).map((l) =>
        h("option", { value: l.id, selected: target && l.id === target.id }, lineLabel(l))));
    const note = h("input", { class: "input", placeholder: T("flagNote"), maxlength: "500" });
    return h("div", { class: "stack" },
      h("label", null, T("flagWhich")), which,
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
  app.order = { ...app.order, open_flags: (app.order.open_flags || 0) + 1 };
  toast(T("flagSent"), "ok");
  renderPickBody();
  if (app.sync) app.sync.kick();
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
  if (e.key === "Enter" || e.key === "Tab") {
    const code = app.wedgeBuffer;
    app.wedgeBuffer = "";
    if (code.length >= 3) {
      e.preventDefault();
      if (app.screen === "pick") handleScan(code);
      else if (app.screen === "orders") openFromCode(code);
    }
    return;
  }
  if (e.key.length !== 1) return;
  if (now - app.wedgeAt > 150) app.wedgeBuffer = "";
  app.wedgeBuffer += e.key;
  app.wedgeAt = now;
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
  for (const o of resp.events) {
    if (o.status !== "error") continue;
    if (o.error && o.error.code === "order_cancelled") {
      toast(T("orderCancelled"), "bad", 9000);
      FX.play("bad");
    }
  }
  if (resp.access && !resp.access.allowed) app.locked = resp.access.message;
  for (const id of needRefetch) {
    try {
      const fresh = await fetchOrder(id);
      if (app.order && app.order.id === id) app.order = fresh;
    } catch {
      break;
    }
  }
  if (app.screen === "pick" && app.order && !app.overlay && !document.querySelector("dialog[open]")) {
    renderPickBody();
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
  });
  app.sync.start();
}

// ---------------------------------------------------------------------------
// End of shift, and dead ends
// ---------------------------------------------------------------------------

async function endShift() {
  stopCamera();
  const left = app.sync ? await app.sync.flush() : await store.outboxCount().catch(() => 0);
  let summary = null;
  try {
    summary = await api("/api/worker/summary");
    await api("/api/worker/logout", { method: "POST" });
  } catch {
    summary = null;
  }
  const name = app.session ? app.session.workerName : "";
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
          stat(summary.orders_completed, T("summaryOrders")))
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
  setLang(saved || ((navigator.language || "").toLowerCase().startsWith("es") ? "es" : "en"));
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
