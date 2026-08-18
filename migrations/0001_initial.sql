-- Autorack Verify: initial schema.
-- SQLite, WAL mode, foreign keys ON (enforced by db.py at connect time).
--
-- Append-only tables (scans, audit_log, billing_events) have BEFORE
-- UPDATE/DELETE triggers that hard-abort any attempt to mutate history.
-- Corrections are new rows (in `exceptions`), never edits.

CREATE TABLE accounts (
    id                    INTEGER PRIMARY KEY,
    name                  TEXT NOT NULL,
    plan                  TEXT NOT NULL DEFAULT 'standard',
    price_per_catch_cents INTEGER NOT NULL,
    free_allowance        INTEGER NOT NULL,
    loose_match_enabled   INTEGER NOT NULL DEFAULT 0,
    loose_suffix_len      INTEGER NOT NULL DEFAULT 8,
    worker_self_resolve   INTEGER NOT NULL DEFAULT 0,
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    status                TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','suspended','cancelled'))
);

CREATE TABLE users (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    email       TEXT NOT NULL UNIQUE,
    pw_hash     TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'owner' CHECK (role IN ('owner','manager')),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_users_account ON users(account_id);

CREATE TABLE workers (
    id           INTEGER PRIMARY KEY,
    account_id   INTEGER NOT NULL REFERENCES accounts(id),
    display_name TEXT NOT NULL,
    first_seen   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_seen    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_workers_account ON workers(account_id);

CREATE TABLE manifests (
    id              INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES accounts(id),
    ref             TEXT NOT NULL,
    source_filename TEXT,
    line_count      INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','committed','archived')),
    bundle_version  INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_manifests_account ON manifests(account_id);

CREATE TABLE manifest_lines (
    id           INTEGER PRIMARY KEY,
    manifest_id  INTEGER NOT NULL REFERENCES manifests(id),
    line_no      INTEGER NOT NULL,
    sku          TEXT,
    description  TEXT,
    qty_expected INTEGER NOT NULL DEFAULT 1,
    raw_barcode  TEXT NOT NULL
);
CREATE INDEX idx_manifest_lines_manifest ON manifest_lines(manifest_id);

-- The match index. tier is the Tier IntEnum value from barcode.py (0-6).
CREATE TABLE line_keys (
    manifest_line_id INTEGER NOT NULL REFERENCES manifest_lines(id),
    tier             INTEGER NOT NULL,
    key              TEXT NOT NULL,
    collision        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_line_keys_lookup ON line_keys(manifest_line_id, tier, key);
CREATE INDEX idx_line_keys_tier_key ON line_keys(tier, key);

CREATE TABLE aliases (
    id             INTEGER PRIMARY KEY,
    account_id     INTEGER NOT NULL REFERENCES accounts(id),
    normalized_key TEXT NOT NULL,
    sku            TEXT NOT NULL,
    confirmed_by   TEXT NOT NULL,
    confirmed_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_aliases_account_key ON aliases(account_id, normalized_key);

CREATE TABLE shifts (
    id               INTEGER PRIMARY KEY,
    account_id       INTEGER NOT NULL REFERENCES accounts(id),
    label            TEXT NOT NULL,
    date             TEXT NOT NULL,
    bundle_hash      TEXT,
    bundle_version   INTEGER NOT NULL DEFAULT 0,
    token            TEXT NOT NULL UNIQUE,
    token_expires_at TEXT NOT NULL,
    revoked_at       TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_shifts_account ON shifts(account_id);
CREATE INDEX idx_shifts_token ON shifts(token);

CREATE TABLE shift_manifests (
    shift_id    INTEGER NOT NULL REFERENCES shifts(id),
    manifest_id INTEGER NOT NULL REFERENCES manifests(id),
    PRIMARY KEY (shift_id, manifest_id)
);

CREATE TABLE sessions (
    id             INTEGER PRIMARY KEY,
    shift_id       INTEGER NOT NULL REFERENCES shifts(id),
    worker_id      INTEGER NOT NULL REFERENCES workers(id),
    device_ua      TEXT,
    started_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_seen      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    clock_skew_ms  INTEGER
);
CREATE INDEX idx_sessions_shift ON sessions(shift_id);

-- Append-only. uuid is the client-generated UUIDv4; idempotency key.
CREATE TABLE scans (
    uuid             TEXT PRIMARY KEY,
    session_id       INTEGER NOT NULL REFERENCES sessions(id),
    manifest_line_id INTEGER REFERENCES manifest_lines(id),
    raw_payload      TEXT NOT NULL,
    normalized       TEXT NOT NULL,
    matched_tier     INTEGER,
    result           TEXT NOT NULL CHECK (result IN ('ok','reject','duplicate','unresolved')),
    decode_ms        REAL,
    match_ms         REAL,
    ts_client        TEXT NOT NULL,
    ts_server        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    bundle_version   INTEGER NOT NULL,
    seq              INTEGER,
    device_ua        TEXT
);
CREATE INDEX idx_scans_session ON scans(session_id);
CREATE INDEX idx_scans_line ON scans(manifest_line_id);
CREATE INDEX idx_scans_ts_server ON scans(ts_server);

CREATE TRIGGER trg_scans_no_update BEFORE UPDATE ON scans
BEGIN SELECT RAISE(ABORT, 'scans are append-only: no UPDATE'); END;

CREATE TRIGGER trg_scans_no_delete BEFORE DELETE ON scans
BEGIN SELECT RAISE(ABORT, 'scans are append-only: no DELETE'); END;

CREATE TABLE exceptions (
    id              INTEGER PRIMARY KEY,
    scan_uuid       TEXT NOT NULL REFERENCES scans(uuid),
    kind            TEXT NOT NULL CHECK (kind IN ('unresolved','tier6_confirm','manual_review','worker_reported')),
    resolved_by     TEXT,
    resolved_at     TEXT,
    resolution_note TEXT,
    manifest_line_id INTEGER REFERENCES manifest_lines(id),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_exceptions_scan ON exceptions(scan_uuid);

CREATE TABLE billing_events (
    id         INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    scan_uuid  TEXT NOT NULL REFERENCES scans(uuid),
    kind       TEXT NOT NULL CHECK (kind IN ('catch','reversal')),
    cents      INTEGER NOT NULL,
    billable   INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    invoice_id INTEGER,
    reversal_reason TEXT
);
CREATE INDEX idx_billing_events_account ON billing_events(account_id);
CREATE UNIQUE INDEX idx_billing_events_scan_kind ON billing_events(scan_uuid, kind);

-- Billing integrity: a "catch" is a prevented mis-ship, i.e. a REJECT --
-- never an 'ok' or 'duplicate' scan, since those are correct/expected
-- scans and are never billed regardless of which tier matched them.
-- result='reject' is only ever set (see static/js/worker/app.js) when
-- every tier came back with zero candidates -- a confident, unambiguous
-- absence from the manifest. result='unresolved' is reserved for the
-- case where some tier hit the ambiguity guard (2+ candidates) before
-- falling through; that is billing-uncertain by construction and can
-- only become billable via an owner-confirmed exception. This is why
-- billing_events gates on scans.result, not matched_tier -- a rejected
-- scan never has a matched_tier at all (nothing was resolved).
CREATE TRIGGER trg_billing_events_certain_tier
BEFORE INSERT ON billing_events
FOR EACH ROW
WHEN NEW.kind = 'catch' AND NOT EXISTS (
    SELECT 1 FROM scans s
    WHERE s.uuid = NEW.scan_uuid
      AND (
        s.result = 'reject'
        OR EXISTS (
            SELECT 1 FROM exceptions e
            WHERE e.scan_uuid = s.uuid AND e.resolved_at IS NOT NULL
        )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'billing_events requires a confident reject or an owner-confirmed exception');
END;

CREATE TRIGGER trg_billing_events_no_update BEFORE UPDATE ON billing_events
BEGIN SELECT RAISE(ABORT, 'billing_events are append-only: no UPDATE; use a reversal row'); END;

CREATE TRIGGER trg_billing_events_no_delete BEFORE DELETE ON billing_events
BEGIN SELECT RAISE(ABORT, 'billing_events are append-only: no DELETE; use a reversal row'); END;

-- Goodwill/administrative credits, separate from the scan-tied
-- billing_events ledger (which the ingest trigger requires a scan_uuid
-- for). Net amount owed = SUM(billing_events.cents) - SUM(account_credits.cents).
CREATE TABLE account_credits (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    cents       INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    granted_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_account_credits_account ON account_credits(account_id);

CREATE TABLE audit_log (
    id           INTEGER PRIMARY KEY,
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,
    target_table TEXT,
    target_id    TEXT,
    before_json  TEXT,
    after_json   TEXT,
    ip           TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_audit_log_created ON audit_log(created_at);
CREATE INDEX idx_audit_log_target ON audit_log(target_table, target_id);

CREATE TRIGGER trg_audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only: no UPDATE'); END;

CREATE TRIGGER trg_audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only: no DELETE'); END;

-- Single-use, short-lived impersonation login links (operator GUI -> web).
CREATE TABLE impersonation_tokens (
    token       TEXT PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    user_id     INTEGER REFERENCES users(id),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    expires_at  TEXT NOT NULL,
    redeemed_at TEXT,
    redeemed_ip TEXT
);

-- Web session cookies for the owner app (separate from shift `sessions`,
-- which track worker scan sessions).
CREATE TABLE web_sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    expires_at TEXT NOT NULL
);
CREATE INDEX idx_web_sessions_user ON web_sessions(user_id);
