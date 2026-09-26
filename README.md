# Autorack

**Warehouse picking verification.** Workers scan each pick with the phone in
their pocket; Autorack checks it against the order instantly (green for right,
red for wrong) and the owner watches mistakes get caught on a live dashboard.
It sits alongside whatever WMS a warehouse already runs. No integration: orders
come in as a CSV pick list.

Flat **$175/month per warehouse**. 14-day trial, free pilots.

```
 Worker's phone (PWA)        Owner's browser (dashboard)
          \                         /
           \   HTTPS / JSON        /
            v                     v
          FastAPI backend (Render / Railway / Fly)
                      |
                Neon Postgres
```

| Surface | Where | What it does |
|---|---|---|
| Worker PWA | `frontend/w/` | Link phone, PIN sign-in, pick orders, scan (camera or Bluetooth wedge), works offline, English/Spanish |
| Owner dashboard | `frontend/app/` | Live floor, mistakes caught, CSV import, pick sheets, workers, phones, insights, billing, settings |
| Landing page | `frontend/index.html` | Marketing + pricing |
| API | `backend/autorack/` | Auth, matching, orders, scan sync, dashboard, Stripe |

## How a shift works

1. **Import orders.** The owner uploads the CSV pick list their system already
   exports (`order_number, barcode, quantity, description`, plus optional `sku`,
   `location`). Headers like "Order #", "UPC", "Qty", "Bin" are recognised. A
   preview shows exactly what will be created, and re-importing the same file
   creates nothing twice.
2. **Print pick sheets** (optional). Sorted by bin location, one QR per order.
3. **Link phones once.** Scan the setup QR on the Phones page (or print the
   setup poster for the break room). The phone becomes an installable app.
4. **Workers sign in with a 4-digit PIN**, open an order by scanning its sheet
   (or the order-number barcode their WMS prints, or from the list), and scan
   every unit. Three units means three scans.
5. **Every scan gets one answer, full-screen**, with sound and vibration:
   - **Right item** (green): counted.
   - **Wrong item** (red): not on this order; put it back.
   - **Already have enough** (amber): the line is complete; put the extra back.
   - **Can't verify** (amber): ambiguous; set it aside for a supervisor.
6. Workers can **undo** a miscount, **flag a problem** (out of stock,
   damaged, label won't scan) and report a **short pick** ("found only 2 of
   3", with a reason). Either can carry **photos**. A flagged order waits for a
   manager or supervisor, who resolves it, ships it short, or sends it back to
   be picked.
7. **Pack and ship** (optional, or required per warehouse): after picking,
   the worker scans the parcel's shipping label. The order is tied to its
   tracking number (carrier recognised) and a printable **proof of shipment**
   lists every unit scanned, by whom and when.
8. The owner sees orders in motion, mistakes caught and **money saved**
   (mistakes × their cost of one mis-ship), problems waiting with their
   photos, and which workers or SKUs keep causing trouble; **reports** by
   customer, item, worker and day over any range (print to PDF); an optional
   **floor board** with a TV mode.
9. **Emails**: a daily summary at the hour each warehouse picks, instant
   alerts for flagged problems and error-rate spikes, and trial-ending /
   payment-failed notices. Each person chooses which they get.

Roles: **owner** (everything incl. billing), **manager** (orders, workers,
phones), **supervisor** (watches and resolves problems; read-only otherwise).
One sign-in can run **several warehouses** and switch between them.

New warehouses get a **setup checklist** and can load 12 sample orders with a
printable barcode sheet, to try scanning before importing anything.

The **operator console** at `/admin/` (for emails in `OPERATOR_EMAILS`) shows
every warehouse: sign-ups, trials ending, pilots and whether they use it,
feature usage, the photos workers take, and activity; it can make a warehouse
a pilot or extend its trial.

## Design decisions (and where they differ from the design doc)

The build follows *Autorack System Design & Technical Architecture*. Where it
goes further, it's because the doc's own goals demanded it:

- **Matching is not a plain string compare.** The doc proposes exact string
  equality. In practice the same carton scans as UPC-E on one phone and appears
  as UPC-A in the CSV, Excel adds whitespace, GS1 case labels wrap the GTIN, and
  exports drop check digits. Each of those would be a red flash on the right
  item, which is how workers learn to ignore the tool. The 7-tier engine
  (`backend/autorack/matching.py`) treats them as the same product, **never
  guesses between candidates** (ambiguous scans go to review), and learns
  owner-taught aliases.
- **Orders, not "the current line".** A scan is matched against the whole
  order, so workers can pick in walking order. The line they were aiming at is
  still recorded, which makes "which SKUs get mis-picked" answerable.
- **Offline is the only path.** Every scan is decided on the phone, written to
  an IndexedDB outbox, then synced. A live scan is a batch of one. The server
  re-decides every scan with the same engine and is authoritative; if it
  disagrees with the phone, the worker is told what to fix physically.
- **Devices + PINs.** A 4-digit PIN is only 10,000 guesses, so PINs only work
  on a phone that was linked to the warehouse, failures lock out, and PIN
  lookup uses a keyed HMAC so a leaked database can't be brute-forced offline.
- **Append-only history is enforced by the database** (triggers), not by
  convention. Undo is a new "void" record, never an edit.
- **Past-due grace.** Stripe retries failed cards for days; locking a warehouse
  mid-shift on the first failure is harsh. There's a configurable grace period,
  and scans already made always sync even when locked.

## Local development

Requirements: Python 3.12+, PostgreSQL 14+, Node 20+ (tests only).

```bash
createdb autorack_dev && createdb autorack_test

cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env              # defaults work for local dev

alembic upgrade head
python -m autorack.cli seed-demo  # demo warehouse, 3 workers, 4 orders
uvicorn autorack.main:app --reload
```

`seed-demo` prints a one-time sign-in link and the phone setup link. With the
default `EMAIL_BACKEND=console`, new sign-in links are printed to the API log.

- Landing page: http://localhost:8000/
- Dashboard: http://localhost:8000/app/
- Worker app: http://localhost:8000/w/ (demo PINs: Maria 2580, Devon 1470, Sam 3690)
- API docs: http://localhost:8000/api/docs

Phone cameras need HTTPS. To test on a real phone, tunnel the dev server
(e.g. `cloudflared tunnel --url http://localhost:8000`) and set `FRONTEND_URL`
and `CORS_ORIGINS` to the tunnel URL. A USB/Bluetooth barcode scanner works on
plain HTTP.

### Tests

```bash
cd backend
ruff check . && ruff format --check . && mypy
pytest -q                       # against TEST_DATABASE_URL (real Postgres)

cd ../frontend && npm test      # node --test, no dependencies
```

Highlights of what's tested:

- `test_tenant_isolation.py` walks **every** route with an id in it, using
  another warehouse's ids, and requires 404. A new route fails the test until
  it's added to the sweep.
- `test_js_parity.py` runs the Python and JavaScript matching engines over an
  adversarial corpus and requires identical keys and decisions.
- `test_scans.py` covers idempotent re-sync, offline ordering, attribution
  after a phone changes hands, and two phones racing for the last unit.
- `test_billing.py` verifies real Stripe webhook signatures, out-of-order
  delivery, grace periods, and that locked accounts still sync history.

## Repository layout

```
backend/
  autorack/
    main.py            app factory, security headers, error shape
    config.py          settings (env vars) + production safety checks
    models.py          SQLAlchemy models (the system of record)
    matching.py        7-tier barcode engine (twin: frontend/shared/barcode.js)
    security.py        tokens, PIN hashing and fingerprints
    deps.py            who's calling, tenant scoping, subscription gate
    api/               routers: auth, warehouse, people, orders, reporting, worker
    services/          scans (sync), orders, csv_import, dashboard, billing, auth, email, ...
    cli.py             operator commands
  alembic/             migrations (incl. append-only triggers)
  tests/
frontend/              static site, no build step (Cloudflare Pages)
  index.html           landing page
  app/                 owner dashboard (hash-routed SPA)
  w/                   worker PWA + service worker
  shared/              API client, DOM helpers, barcode engine
  _headers, build.sh   Pages headers/CSP and API configuration
docs/                  deployment, architecture, operations; docs/brand/ has the original logo files
```

### Brand

The UI follows the logo: navy `#162238`, blue `#3E7BFA`, off-white `#F7F7F5`,
and Inter (self-hosted in `frontend/assets/fonts/` under the SIL Open Font
License, so the phone app renders it offline). The mark is redrawn as SVG in
`frontend/assets/brand/` (`mark.svg` on light backgrounds, `mark-reverse.svg`
on navy); app icons in `frontend/assets/icons/` are generated from it. Green,
red and amber are reserved for scan results.

## More

- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md): Neon + Render/Railway/Fly + Cloudflare Pages + Stripe + email, step by step
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): data model, scan pipeline, offline sync, security
- [docs/OPERATIONS.md](docs/OPERATIONS.md): onboarding a pilot, support tasks, the CLI
