// Pure picking logic -- no DOM, no storage, no network -- so it runs the same
// on the phone and under `node --test`.
//
// The model: the server's last known line quantities are the baseline, and
// every event still waiting in the outbox is replayed on top of it for
// display. After a sync the baseline moves forward and those events leave
// the outbox, so the numbers on screen never double-count and never go
// backwards unless the server actually disagreed.

import { buildIndex, matchAgainstIndex, normalizedKey, parseGs1 } from "../../shared/barcode.js";

export const ORDER_QR_PREFIX = "AUTORACK:ORDER:";

export function isOrderCode(text) {
  return String(text).trim().toUpperCase().startsWith(ORDER_QR_PREFIX);
}

export function orderIdFromCode(text) {
  const t = String(text).trim();
  if (!isOrderCode(t)) return null;
  const id = t.slice(ORDER_QR_PREFIX.length).toLowerCase();
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(id) ? id : null;
}

const matcherCache = new WeakMap();

function matcherFor(order) {
  let m = matcherCache.get(order);
  if (!m) {
    m = {
      index: buildIndex(order.match.index),
      options: {
        looseMatchEnabled: order.match.loose_match_enabled,
        suffixLen: order.match.suffix_len,
        disabledKeys: order.match.disabled_keys || {},
      },
    };
    matcherCache.set(order, m);
  }
  return m;
}

/**
 * Receiving, returns and counts tally what's there: every listed item counts
 * (even past the expected quantity), anything else is an "extra", and the
 * worker finishes the job. Picks police the order.
 */
export function isTally(order) {
  return Boolean(order && order.kind && order.kind !== "pick");
}

/** Expected vs counted per line, and extras, for the finish screen. */
export function tallySummary(lines, extras) {
  let over = 0;
  let short = 0;
  let counted = 0;
  for (const l of lines) {
    counted += l.scanned_quantity;
    if (l.scanned_quantity > l.expected_quantity) over += 1;
    else if (l.scanned_quantity < l.expected_quantity) short += 1;
  }
  return { counted, over, short, extras, matches: over === 0 && short === 0 && extras === 0 };
}

/** Extras queued or confirmed this session that haven't been undone. */
export function extrasFor(history, orderId) {
  const voided = new Set(history.filter((h) => h.kind === "void").map((h) => h.target));
  return history.filter((h) => h.orderId === orderId && h.kind === "scan" && h.result === "extra" && !voided.has(h.id)).length;
}

/** Units still to pick on a line: expected, minus picked, minus reported short. */
export function remaining(line) {
  return Math.max(0, line.expected_quantity - line.scanned_quantity - (line.short_quantity || 0));
}

/** Line quantities as the worker should see them: server baseline + queue. */
export function displayLines(order, pending) {
  const qty = new Map(order.lines.map((l) => [l.id, l.scanned_quantity]));
  const short = new Map(order.lines.map((l) => [l.id, l.short_quantity || 0]));
  const expected = new Map(order.lines.map((l) => [l.id, l.expected_quantity]));
  const ordered = pending.filter((e) => e.order_id === order.id).sort((a, b) => a.client_seq - b.client_seq);
  for (const ev of ordered) {
    const lineId = (ev.local && ev.local.lineId) || (ev.kind === "short" && ev.line_item_id);
    if (!lineId || !qty.has(lineId)) continue;
    const units = (ev.local && ev.local.qty) || 1;
    const counts = ev.kind === "scan" || ev.kind === "confirm";
    if (counts && ev.local && (ev.local.result === "match" || ev.local.result === "counted")) {
      qty.set(lineId, qty.get(lineId) + units);
    }
    if (ev.kind === "void") qty.set(lineId, Math.max(0, qty.get(lineId) - units));
    if (ev.kind === "short") {
      const left = Math.max(0, expected.get(lineId) - qty.get(lineId) - short.get(lineId));
      short.set(lineId, short.get(lineId) + Math.min(ev.quantity || 0, left));
    }
  }
  return order.lines.map((l) => ({ ...l, scanned_quantity: qty.get(l.id), short_quantity: short.get(l.id) }));
}

/** Picked + reported short, against expected. Short units count as accounted for. */
export function progress(lines) {
  let done = 0;
  let total = 0;
  let short = 0;
  for (const l of lines) {
    total += l.expected_quantity;
    const s = l.short_quantity || 0;
    short += s;
    done += Math.min(l.scanned_quantity + s, l.expected_quantity);
  }
  return { done, total, short, complete: total > 0 && done >= total };
}

/** Has this order's label been scanned (queued or confirmed)? */
export function shippedTracking(order, pending) {
  const queued = pending.find((e) => e.order_id === order.id && e.kind === "ship");
  if (queued) return queued.tracking_number;
  return order.status === "shipped" ? order.tracking_number || "" : null;
}

/**
 * A shipping-label scan, checked before it's queued: a product barcode from
 * this order means the worker scanned the wrong thing.
 */
export function checkLabel(order, raw) {
  const t = String(raw || "").replace(/[\s-]/g, "").toUpperCase();
  if (t.length < 8) return { ok: false, reason: "short" };
  const { index, options } = matcherFor(order);
  const m = matchAgainstIndex(index, raw, options);
  if (m.resolved || m.ambiguous) return { ok: false, reason: "product" };
  return { ok: true, tracking: t };
}

/**
 * Decide a scan's result locally, exactly as the server will:
 *   match      resolved with confidence, line still needs units
 *   over_pick  resolved with confidence, line already complete
 *   review     low-confidence tier, or ambiguous between lines
 *   mismatch   confidently not on this order
 */
/**
 * What one scan of `raw` stands for, beyond the line it matched: a case
 * barcode counts its pack size (`qty`), and an approved substitute is
 * marked (`sub`: the substitute's name). Mirrors services/scans.py.
 */
export function scanExtras(order, raw) {
  const key = normalizedKey(String(raw || ""));
  const packs = (order.match && order.match.packs) || {};
  const subs = (order.match && order.match.subs) || {};
  const out = {};
  if (packs[key] > 1) out.qty = packs[key];
  if (subs[key]) out.sub = subs[key];
  return out;
}

export function classify(order, lines, raw) {
  const { index, options } = matcherFor(order);
  const m = matchAgainstIndex(index, raw, options);
  const extras = m.resolved ? scanExtras(order, raw) : {};
  const units = extras.qty || 1;
  if (isTally(order)) {
    // counted | extra | review, as the server decides for tally jobs.
    if (m.resolved && !m.needsConfirmation && lines.some((l) => l.id === m.lineId)) {
      return { result: "counted", lineId: m.lineId, tier: m.tier, ...extras };
    }
    if (m.resolved || m.ambiguous) return { result: "review", lineId: m.resolved ? m.lineId : null, tier: m.tier };
    return { result: "extra", lineId: null, tier: null };
  }
  if (m.resolved && !m.needsConfirmation) {
    const line = lines.find((l) => l.id === m.lineId);
    if (line) {
      // A whole case when fewer units are left is an over-pick: open it.
      const enough = remaining(line) >= units;
      return {
        result: enough ? "match" : "over_pick",
        lineId: line.id,
        tier: m.tier,
        ...(enough ? extras : extras.qty ? { qty: extras.qty } : {}),
      };
    }
  }
  if (m.resolved || m.ambiguous) return { result: "review", lineId: m.resolved ? m.lineId : null, tier: m.tier };
  return { result: "mismatch", lineId: null, tier: null };
}

/**
 * Batch picking: which order a scanned item goes to. `entries` are
 * [{order, lines}] in tote order. The order of the line being picked wins if
 * it still needs the item, then the others in tote order; if none needs it,
 * the scan is judged against the order being picked. Returns {order, c}.
 */
export function classifyBatch(entries, raw, preferId) {
  const ordered = entries.slice().sort((a, b) => Number(b.order.id === preferId) - Number(a.order.id === preferId));
  let fallback = null;
  for (const { order, lines } of ordered) {
    const c = classify(order, lines, raw);
    if (c.result === "match") return { order, c };
    if (!fallback && (c.result === "over_pick" || c.result === "review")) fallback = { order, c };
  }
  if (fallback) return fallback;
  const first = ordered[0];
  return { order: first.order, c: classify(first.order, first.lines, raw) };
}

/** The line to pick next: the worker's choice if unfinished, else walk by location. */
export function nextLine(lines, preferredId) {
  const open = lines.filter((l) => remaining(l) > 0);
  const preferred = open.find((l) => l.id === preferredId);
  if (preferred) return preferred;
  return sortForWalking(open)[0] || null;
}

export function sortForWalking(lines) {
  return lines.slice().sort((a, b) => {
    const la = a.location || "￿";
    const lb = b.location || "￿";
    if (la !== lb) return la < lb ? -1 : 1;
    return a.line_no - b.line_no;
  });
}

/** Fold a sync response's order state into the cached order. */
export function applyServerState(order, state) {
  if (!state) return { order, needsRefetch: false };
  const shorts = state.short || {};
  const updated = {
    ...order,
    status: state.status,
    open_flags: state.open_flags,
    tracking_number: state.tracking_number === undefined ? order.tracking_number : state.tracking_number,
    lines: order.lines.map((l) =>
      Object.prototype.hasOwnProperty.call(state.lines, l.id)
        ? { ...l, scanned_quantity: state.lines[l.id], short_quantity: shorts[l.id] || 0 }
        : l,
    ),
  };
  const sameLines =
    Object.keys(state.lines).length === order.lines.length && order.lines.every((l) => l.id in state.lines);
  // A version bump can mean new lines, a taught alias or new match settings:
  // anything that changes the index. Keep the new quantities now, and fetch
  // the full order when there's a connection.
  const needsRefetch = !sameLines || state.version !== order.version;
  return { order: updated, needsRefetch };
}

/**
 * Compare what the phone told the worker with what the server decided, and
 * describe any difference that the worker has to act on physically.
 * Returns [{kind, key, vars}] for the UI to translate.
 */
export function corrections(events, outcomes, linesById) {
  const byId = new Map(events.map((e) => [e.id, e]));
  const out = [];
  for (const o of outcomes) {
    const ev = byId.get(o.id);
    if (!ev || ev.kind !== "scan" || o.status !== "applied" || !ev.local) continue;
    if (o.result === ev.local.result) continue;
    const line = linesById.get(o.line_item_id || ev.local.lineId);
    const item = line ? line.description || line.sku || line.expected_barcode : ev.scanned_barcode;
    const vars = { item, code: ev.scanned_barcode };
    if (o.result === "over_pick") out.push({ kind: "warn", key: "correctionOverPick", vars });
    else if (o.result === "mismatch") out.push({ kind: "bad", key: "correctionMismatch", vars });
    else if (o.result === "review") out.push({ kind: "warn", key: "correctionReview", vars });
    else if (o.result === "match" || o.result === "counted") out.push({ kind: "ok", key: "correctionMatch", vars });
    else if (o.result === "extra") out.push({ kind: "warn", key: "correctionExtra", vars });
  }
  return out;
}

/** The most recent counted scan in this order that hasn't been undone. */
export function lastUndoable(history, orderId) {
  const voided = new Set(history.filter((h) => h.kind === "void").map((h) => h.target));
  for (let i = history.length - 1; i >= 0; i--) {
    const h = history[i];
    const undoable = h.result === "match" || h.result === "counted" || h.result === "extra";
    if (h.orderId === orderId && h.kind === "scan" && undoable && !voided.has(h.id)) return h;
  }
  return null;
}

/** Fields the sync API accepts; local bookkeeping stays on the phone. */
export const WIRE_FIELDS = [
  "id", "kind", "order_id", "session_id", "client_scanned_at", "client_seq", "scanned_barcode",
  "intended_line_item_id", "client_result", "offline", "target_scan_id", "line_item_id", "scan_event_id",
  "reason", "note", "quantity", "short_reason", "tracking_number", "lot", "serial", "expiry",
];

export function toWire(ev) {
  const out = {};
  for (const k of WIRE_FIELDS) if (ev[k] !== undefined && ev[k] !== null) out[k] = ev[k];
  return out;
}

/**
 * Should an event leave the outbox after this response? Yes when the server
 * accepted it or refused it for good; no when the refusal might succeed
 * later (it never does, today, but a future transient code shouldn't lose
 * scans).
 */
export function isSettled(outcome) {
  return outcome.status === "applied" || outcome.status === "duplicate" || outcome.status === "error";
}

// ---------------------------------------------------------------------------
// Lot / serial / expiry (mirrors services/scans.py)
// ---------------------------------------------------------------------------

/** GS1 YYMMDD -> "YYYY-MM-DD"; day 00 means the month's last day. */
export function gs1Date(yymmdd) {
  if (!/^\d{6}$/.test(yymmdd || "")) return null;
  const y = 2000 + Number(yymmdd.slice(0, 2));
  const m = Number(yymmdd.slice(2, 4));
  let d = Number(yymmdd.slice(4, 6));
  if (m < 1 || m > 12) return null;
  const last = new Date(Date.UTC(y, m, 0)).getUTCDate();
  if (d === 0) d = last;
  if (d > last) return null;
  return `${y}-${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
}

/** What the barcode itself carries (GS1 AIs 10, 21, 17/15). */
export function unitDetails(raw) {
  const g = parseGs1(String(raw || ""));
  if (!g) return { lot: null, serial: null, expiry: null };
  return {
    lot: g.lot || null,
    serial: g.serial || null,
    expiry: gs1Date(g.extra["17"] || g.extra["15"] || null),
  };
}

/** Which details this line needs that the barcode didn't carry. */
export function neededDetails(line, details) {
  const need = [];
  if (!line) return need;
  if ((line.track_lot || line.required_lot) && !details.lot) need.push("lot");
  if (line.track_serial && !details.serial) need.push("serial");
  if (line.track_expiry && !details.expiry) need.push("expiry");
  return need;
}

/**
 * Why this unit of the right product must not ship (picks only): wrong_lot,
 * expired, or serial_repeat (already picked on this phone; the server also
 * checks every other order).
 */
export function traceProblem(line, details, history, today) {
  if (line.required_lot && String(details.lot || "").trim().toUpperCase() !== line.required_lot.trim().toUpperCase()) {
    return "wrong_lot";
  }
  if (details.expiry && details.expiry < today) return "expired";
  if (line.track_serial && details.serial) {
    const voided = new Set(history.filter((h) => h.kind === "void").map((h) => h.target));
    const repeat = history.some((h) => h.kind === "scan" && h.result === "match" && h.lineId === line.id
      && h.serial === details.serial && !voided.has(h.id));
    if (repeat) return "serial_repeat";
  }
  return null;
}

/** Today in the phone's local time, as YYYY-MM-DD. */
export function localToday(now = new Date()) {
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
}
