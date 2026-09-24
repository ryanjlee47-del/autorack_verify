// Pure picking logic -- no DOM, no storage, no network -- so it runs the same
// on the phone and under `node --test`.
//
// The model: the server's last known line quantities are the baseline, and
// every event still waiting in the outbox is replayed on top of it for
// display. After a sync the baseline moves forward and those events leave
// the outbox, so the numbers on screen never double-count and never go
// backwards unless the server actually disagreed.

import { buildIndex, matchAgainstIndex } from "../../shared/barcode.js";

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

/** Line quantities as the worker should see them: server baseline + queue. */
export function displayLines(order, pending) {
  const qty = new Map(order.lines.map((l) => [l.id, l.scanned_quantity]));
  const ordered = pending.filter((e) => e.order_id === order.id).sort((a, b) => a.client_seq - b.client_seq);
  for (const ev of ordered) {
    const lineId = ev.local && ev.local.lineId;
    if (!lineId || !qty.has(lineId)) continue;
    if (ev.kind === "scan" && ev.local.result === "match") qty.set(lineId, qty.get(lineId) + 1);
    if (ev.kind === "void") qty.set(lineId, Math.max(0, qty.get(lineId) - 1));
  }
  return order.lines.map((l) => ({ ...l, scanned_quantity: qty.get(l.id) }));
}

export function progress(lines) {
  let done = 0;
  let total = 0;
  for (const l of lines) {
    total += l.expected_quantity;
    done += Math.min(l.scanned_quantity, l.expected_quantity);
  }
  return { done, total, complete: total > 0 && done >= total };
}

/**
 * Decide a scan's result locally, exactly as the server will:
 *   match      resolved with confidence, line still needs units
 *   over_pick  resolved with confidence, line already complete
 *   review     low-confidence tier, or ambiguous between lines
 *   mismatch   confidently not on this order
 */
export function classify(order, lines, raw) {
  const { index, options } = matcherFor(order);
  const m = matchAgainstIndex(index, raw, options);
  if (m.resolved && !m.needsConfirmation) {
    const line = lines.find((l) => l.id === m.lineId);
    if (line) {
      return {
        result: line.scanned_quantity < line.expected_quantity ? "match" : "over_pick",
        lineId: line.id,
        tier: m.tier,
      };
    }
  }
  if (m.resolved || m.ambiguous) return { result: "review", lineId: m.resolved ? m.lineId : null, tier: m.tier };
  return { result: "mismatch", lineId: null, tier: null };
}

/** The line to pick next: the worker's choice if unfinished, else walk by location. */
export function nextLine(lines, preferredId) {
  const open = lines.filter((l) => l.scanned_quantity < l.expected_quantity);
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
  const updated = {
    ...order,
    status: state.status,
    open_flags: state.open_flags,
    lines: order.lines.map((l) =>
      Object.prototype.hasOwnProperty.call(state.lines, l.id) ? { ...l, scanned_quantity: state.lines[l.id] } : l,
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
    else if (o.result === "match") out.push({ kind: "ok", key: "correctionMatch", vars });
  }
  return out;
}

/** The most recent counted scan in this order that hasn't been undone. */
export function lastUndoable(history, orderId) {
  const voided = new Set(history.filter((h) => h.kind === "void").map((h) => h.target));
  for (let i = history.length - 1; i >= 0; i--) {
    const h = history[i];
    if (h.orderId === orderId && h.kind === "scan" && h.result === "match" && !voided.has(h.id)) return h;
  }
  return null;
}

/** Fields the sync API accepts; local bookkeeping stays on the phone. */
export const WIRE_FIELDS = [
  "id", "kind", "order_id", "session_id", "client_scanned_at", "client_seq", "scanned_barcode",
  "intended_line_item_id", "client_result", "offline", "target_scan_id", "line_item_id", "scan_event_id",
  "reason", "note",
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
