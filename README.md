# Autorack Verify

Barcode verification for small warehouses. It runs alongside whatever WMS or
paper process is already in place: the owner uploads a pick or ship manifest,
floor workers scan items with a phone camera or a Bluetooth wedge scanner, and
the app tells them immediately whether the item belongs on the shipment.

- **Wrong item** → hard reject before it leaves the dock.
- **Already scanned** → duplicate warning.
- **Correct** → confirmed, and the line is marked.

Built for 10–60 workers and no IT staff. A shift needs one printed QR code on
the wall and phones the workers already own. Billing is per error caught: a
number of free catches, then a flat rate per prevented mis-ship.

## Contents

- [How a shift works](#how-a-shift-works)
- [The three surfaces](#the-three-surfaces)
- [The matching engine](#the-matching-engine)
- [Billing](#billing)
- [Architecture](#architecture)
- [Running it locally](#running-it-locally)
- [Deployment](#deployment)
- [Operator GUI](#operator-gui)
- [Backups](#backups)
- [Savings reports](#savings-reports)
- [Development](#development)

## How a shift works

1. The owner uploads a manifest (CSV, TSV, or pasted text). Columns are
   auto-detected; a preview shows what was found before anything is
   committed.
2. The owner prepares a shift and assigns one or more manifests to it. The
   server builds a bundle containing every line and derived lookup key, and
   prints a wall QR code.
3. Workers scan the QR with their phone, type their name, and the shift data
   downloads once. From that point the phone needs no network.
4. Workers scan items. Each scan gets one of three answers — OK, REJECT, or
   DUPLICATE — as a full-screen colour before any text.
5. Scans sync back whenever there is signal. The owner watches the floor view
   live, reviews anything the system could not resolve, and sees running
   savings on the billing dashboard.

## The three surfaces

**Worker PWA** (`/w`) — phone-first and installable, English and Spanish
(chosen on the join screen). Tapping the camera button opens a 15-second scan
window; manual barcode entry is always available. A worker can appeal a
REJECT with a photo, when the account has that option enabled; appeals are
reviewed by the owner before affecting billing. Logging out shows an
end-of-shift summary — scanned, caught, duplicate, needs review.

**Owner web app** — signup, manifest upload, shift preparation, live floor
view, exception review, per-worker statistics, and a savings dashboard.
Manifests stay editable after commit (`/manifests/<id>`, a paginated line
editor); editing regenerates the match index and bumps the bundle version on
any shift using that manifest. `/workers` can rename, deactivate, or merge
workers. Timestamps render in the account's timezone; storage is UTC.

**Operator GUI** (`admin_gui.py`) — a Tkinter desktop app with nine tabs:
accounts, live feed, tables, SQL console, billing, manifests and shifts,
backups, server control, and an overview. Password resets are handled by an
operator in the Accounts tab (email verification, then a generated password
shown once). The same tab creates accounts with their first owner and manages
users on existing accounts.

**Offline handling.** The wall QR carries a single-use token, exchanged once
for a gzipped bundle stored in IndexedDB. Every scan writes to a local outbox
before any network attempt, tagged with a UUID and sequence number, and syncs
in idempotent batches when online. If two workers scan the same line, the
first to reach the server gets the OK and the second becomes a duplicate;
both records are kept — scan history is append-only. A scan made against a
stale bundle is still recorded but not billed until the phone refreshes.

## The matching engine

A scanned payload is normalized (control characters stripped, uppercased,
GS1 element strings parsed, GTINs canonicalized) and looked up across seven
tiers, in this order:

```
RAW → NORMALIZED → GTIN14 → ALIAS → DIGITS_STRIPPED → BODY_NO_CHECK → SUFFIX
```

| Tier | Match on | Confidence |
|---|---|---|
| 0 | Raw string, byte-identical | certain |
| 1 | Normalized string | certain |
| 2 | Canonical GTIN-14 | certain |
| 3 | Digits only, leading zeros stripped | high |
| 4 | Body with check digit removed | high |
| 5 | Learned alias (account-scoped) | certain |
| 6 | Last-N-digit suffix (opt-in) | low |

Matching stops at the first tier producing exactly one candidate. Zero or
several candidates fall through to the next tier; if every tier is exhausted
or ambiguous, the result is *unresolved*. When a manifest is committed, any
loose-tier key shared by more than one line is flagged and excluded from the
bundle.

The engine exists twice — Python on the server, JavaScript on the phone —
kept byte-for-byte identical and covered by a shared parity test suite.

## Billing

Scan results have four values:

- **ok** / **duplicate** — resolved to a real line. Never billable.
- **reject** — every tier returned zero candidates. Confident absence.
  Billable.
- **unresolved** — a tier found several candidates before falling through.
  Never billed automatically; the owner reviews it, teaching an alias or
  confirming a genuine wrong item.

The client classifies a scan while offline; the server independently
rebuilds the index and recomputes the match, and only bills if it agrees. A
database trigger blocks any catch record unless the referenced scan is a
confident reject or carries a resolved exception. Scans, billing records,
and the audit log are all append-only; a billing correction is a reversal
record with a typed reason. Every privileged action — account edits,
credits, manifest commits, shift changes, worker changes, exception
resolutions, logins/logouts, appeals — writes to the audit log.

Pricing constants live in one module (`pricing.py`), read by the billing
engine, the owner dashboard, and the public pricing page.

## Architecture

```
barcode.py             normalization + tiered matching engine (the core)
static/js/barcode.js   the same engine in JS, byte-parity tested
db.py                  SQLite access layer -- named functions, no ORM
sqlstore.py            loads sql/*.sql into SQL["file.name"]
sql/*.sql              every static query, one file per entity
migrations/*.sql       schema, applied in order
manifest_ingest.py     parsing, column detection, collision analysis, bundles
seed.py                demo account + 3 manifests, for local development only
auth.py                PBKDF2 hashing, opaque session tokens
i18n.py                English/Spanish strings for the worker PWA
billing.py             catches, reversals, savings
pricing.py             the one place pricing constants live
backup.py              hot database + appeal-photo backups, retention, restore
reports.py             standalone HTML savings reports
tz.py                  account-timezone display (storage stays UTC)
app.py                 Flask: worker PWA (/w) + owner app + marketing (/)
admin_api.py           operator operations, shared by local and remote GUI
admin_gui.py           Tkinter desktop app -- the operator's admin surface
reset_data.py          wipe operational data, keep the schema
tools/generate_erd.py  regenerates docs/erd.png from the applied schema
templates/, static/    Jinja templates and vanilla CSS/JS per surface
tests/                 pytest, one file per concern
```

Stack: Flask, SQLite, Jinja, vanilla JavaScript. No ORM, no build step, no
CDN dependency.

Static SQL lives in `sql/*.sql`, one file per entity, addressed by name via
`sqlstore.py`:

```sql
-- sql/accounts.sql
-- name: get_account
SELECT * FROM accounts WHERE id = ?;
```

```python
def get_account(conn, account_id):
    return query_one(conn, SQL["accounts.get_account"], (account_id,))
```

## Running it locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest tests/ -q

python serve.py
```

`serve.py` creates the database, seeds demo data, detects the LAN IP,
generates a self-signed certificate, serves over HTTPS, and prints the LAN
URL with an ASCII QR code. Accept the browser's self-signed-certificate
warning to continue (phone cameras require HTTPS or localhost).

```bash
python serve.py --offline-drill
```

Seeds a shift, prints its join QR, and blocks sync with a 503 for 60
seconds, to demonstrate the offline path: join on a real phone, let the
bundle download, switch to airplane mode, scan, then reconnect and watch the
outbox drain into the floor view.

Demo login, printed by `serve.py` on first run:
`owner@dockside-demo.test` / `dockside-demo-2026`.

## Deployment

Run under `gunicorn` with Caddy in front for TLS:

```
verify.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

```bash
gunicorn --workers 4 --bind 127.0.0.1:8000 --access-logfile - "app:create_app()"
```

Run under a systemd unit with `Restart=always`. The application secret is
read from `data/secret_key` (mode `0600`, created on first run) so every
worker process agrees on it; CSRF tokens derive from it. Login/signup rate
limiting is stored in the database rather than per-process memory.

Before exposing signup to the internet: put real TLS in front of it, run
`pip-audit -r requirements.txt` as part of releases, get an independent
security review, decide how signups are gated (no email verification ships
by default), and keep `data/` off any public host path.

## Operator GUI

```bash
python admin_gui.py --db data/app.db
```

Every tab goes through one operation layer that either opens a local
connection or posts the same operation names over HTTP to a remote
deployment.

Remote mode:

```bash
python admin_api_token.py --show   # run on the server once

python admin_gui.py --remote-url https://your-host.example.com --token-file ~/.autorack_admin_token
```

`--token-file` keeps the token out of shell history. In remote mode, server
start/stop controls are disabled, backups download to your machine with the
automatic timer off, and the live feed polls less aggressively.

## Backups

- Backups land in `backup_data/`, never inside `data/`.
- The database half uses SQLite's online backup API (safe against a live
  database in WAL mode).
- `serve.py` takes one on every startup, before running migrations.
- The operator GUI takes one every 30 minutes while open, and has manual
  backup/restore buttons.
- Retention keeps the 30 most recent of each type.
- Worker appeal photos are backed up alongside the database, tagged with the
  same timestamp.

```bash
python reset_data.py --db data/app.db          # dry run
python reset_data.py --db data/app.db --yes    # wipe operational data, keep schema
```

## Savings reports

Triggered from the operator GUI's Billing tab, for one account or all of
them. Each report is a single self-contained HTML file that opens from disk
and prints cleanly to PDF via the browser. Contents: money saved to date,
mis-ships caught, free allowance, net amount owed, and a per-worker
breakdown.

## Development

```bash
pip install -r requirements-dev.txt
ruff check .          # lint
ruff format --check . # formatting
mypy                  # types
pytest -q             # test suite
```

Configuration lives in `pyproject.toml`. CI installs Node so the JS/Python
matching-engine parity tests run, and regenerates `docs/erd.png` and
`docs/schema.json` after schema changes.
