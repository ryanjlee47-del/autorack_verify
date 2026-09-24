# Architecture

## Data model

```
Warehouse ──< User (owner/manager, magic-link sign-in) ──< OwnerSession
    │
    ├──< Device (a linked phone) ──< WorkerSession >── Worker (PIN)
    │
    ├──< Order ──< OrderLineItem
    │       ├──< ScanEvent  (append-only; match | over_pick | mismatch | review | void)
    │       └──< OrderFlag  (worker-raised problem; resolved by an owner)
    │
    ├──< BarcodeAlias (owner-taught "this code means that code")
    ├──< ImportBatch
    └──< AuditLog (append-only)
```

Every tenant-owned row carries `warehouse_id`, even children of rows that
already have one, so every query filters on it directly. Route handlers never
accept a warehouse id from the client; it comes from the authenticated
principal (`deps.py`). `tests/test_tenant_isolation.py` sweeps every id route.

`scan_events` and `audit_log` have `BEFORE UPDATE OR DELETE` triggers that
raise. History can only grow.

## The scan pipeline

```
phone: decode (BarcodeDetector / ZXing / wedge keystrokes)
     → classify locally (shared/barcode.js + w/js/state.js)
     → write to IndexedDB outbox            ← the scan is now durable
     → show green / red / amber, sound, vibration
     → background sync: POST /api/worker/sync (batch ≤ 500)

server: for each order in the batch (row-locked), events in phone-time order:
     → idempotency: event id already stored? return the stored outcome
     → re-run the matching engine against the current order + aliases
     → match / over_pick / mismatch / review; adjust line quantity
     → store ScanEvent (with the phone's claim alongside)
     → recompute order status; return authoritative state
phone: fold server state into the cached order; tell the worker if the
       server's answer differs from what they were shown
```

### Matching tiers

Lookup order: raw → normalized → GTIN-14 → alias → digits-only → body-without-check-digit → suffix (opt-in).
Matching stops at the first tier with **exactly one** candidate line. A tier
with several candidates marks the scan ambiguous and falls through; if nothing
resolves, an ambiguous scan goes to **review** and an unambiguous one is a
**mismatch**. Keys shared by several lines at the loose tiers are disabled at
import, which also marks a hit on them as ambiguous. The suffix tier never
counts a pick on its own; it only sends the scan to review.

The same engine exists in Python (authoritative) and JavaScript (offline
feedback). Both strip an explicit character set rather than using `strip()` or
`trim()`, which disagree about the GS1 separator bytes. `test_js_parity.py`
holds them together.

### Order status

`pending → in_progress` on the first scan; `completed` when every line has its
quantity; `flagged` while any worker flag is open (it outranks completed: a
flagged order needs a human before it ships); `cancelled` by an owner. Any
change a phone's cached copy needs to know about bumps `orders.version`.

## Offline

- **Orders** are cached in IndexedDB with their match index when opened; the
  order list prefetches up to 40 open orders while online.
- **Outbox** holds scans, undos and flags until the server confirms them. Each
  carries the worker session it was made under, so attribution survives a
  session expiring or the phone changing hands before it syncs.
- **Display** = server baseline + replay of the outbox. Numbers never
  double-count and only move backwards when the server disagreed.
- **Service worker** caches the app shell network-first with a 3.5 s timeout,
  so deploys reach phones while a phone in a dead zone still reloads.
- Signing in and first opening an order need a connection; everything after
  that works offline.

## Security

- Owners: single-use magic links (15 min), token in the URL fragment so it
  never reaches server logs; opaque session tokens, stored hashed; revocable.
- Phones: linked with a rotatable join code; device tokens stored hashed;
  revocable from the dashboard.
- PINs: salted PBKDF2 for verification, plus an HMAC fingerprint keyed by
  `SECRET_KEY` for lookup and uniqueness. 5 wrong PINs lock a phone for 10
  minutes, 25 lock the warehouse's PIN entry. Limits are stored in Postgres, so
  they hold across processes and restarts.
- Rate limits on sign-in, sign-up, device linking (Postgres) and on sync/import
  (per process).
- Strict CSP (`script-src 'self'`, no inline code), no `innerHTML` for data,
  CSV exports neutralize spreadsheet formulas.
- Stripe webhooks: signature-verified, idempotent, re-fetch current state.

## Billing and access

`services/access.py` is the single decision for "can this warehouse work right
now?": pilot or active → yes; trial → until it ends; past-due → during the grace
period; otherwise no. Locked warehouses can't sign workers in, open orders on
phones, or create or import orders. They **can** read everything, manage billing,
and sync scans that already happened.
