-- Statements for the `operator` surface.

-- name: list_all_shifts
SELECT * FROM shifts ORDER BY created_at DESC;

-- name: list_all_manifests
SELECT * FROM manifests ORDER BY created_at DESC;

-- name: live_feed
SELECT sc.uuid, sc.ts_server, sc.result, sc.raw_payload, sc.matched_tier, w.display_name AS worker, a.name AS account FROM scans sc JOIN sessions se ON se.id = sc.session_id JOIN workers w ON w.id = se.worker_id JOIN shifts sh ON sh.id = se.shift_id JOIN accounts a ON a.id = sh.account_id ORDER BY sc.ts_server DESC LIMIT 150;

-- name: scans_per_hour_last_24h
SELECT strftime('%H', ts_server) AS hr, COUNT(*) AS n FROM scans WHERE ts_server >= datetime('now', '-24 hours') GROUP BY hr;

-- name: count_open_exceptions
SELECT COUNT(*) AS n FROM exceptions WHERE resolved_at IS NULL;

-- name: count_scans_today_utc
SELECT COUNT(*) AS n FROM scans WHERE date(ts_server) = date('now');

-- name: count_active_shifts
SELECT COUNT(*) AS n FROM shifts WHERE revoked_at IS NULL AND token_expires_at > strftime('%Y-%m-%dT%H:%M:%fZ','now');

-- name: count_accounts
SELECT COUNT(*) AS n FROM accounts;
