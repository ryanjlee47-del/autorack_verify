-- Statements for db.py's `shifts` section.

-- name: create_shift
INSERT INTO shifts (account_id, label, date, token, token_expires_at, bundle_hash, bundle_version) VALUES (?,?,?,?,?,?,?);

-- name: get_shift
SELECT * FROM shifts WHERE id = ?;

-- name: get_shift_by_token
SELECT * FROM shifts WHERE token = ?;

-- name: list_shifts
SELECT * FROM shifts WHERE account_id = ? ORDER BY created_at DESC;

-- name: revoke_shift
UPDATE shifts SET revoked_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?;

-- name: link_shift_manifest
INSERT OR IGNORE INTO shift_manifests (shift_id, manifest_id) VALUES (?,?);

-- name: get_shift_manifest_ids
SELECT manifest_id FROM shift_manifests WHERE shift_id = ?;

-- name: get_shifts_for_manifest
SELECT sh.* FROM shifts sh JOIN shift_manifests sm ON sm.shift_id = sh.id WHERE sm.manifest_id = ?;

-- name: bump_shift_bundle_version
UPDATE shifts SET bundle_version = bundle_version + 1 WHERE id = ?;

-- name: list_sessions_for_shift
SELECT se.*, w.display_name AS worker_name FROM sessions se JOIN workers w ON w.id = se.worker_id WHERE se.shift_id = ? ORDER BY se.started_at DESC;

-- name: create_session
INSERT INTO sessions (shift_id, worker_id, device_ua, token) VALUES (?,?,?,?);

-- name: get_session
SELECT * FROM sessions WHERE id = ?;

-- name: get_session_by_token
SELECT * FROM sessions WHERE token = ?;

-- name: end_session
-- Idempotent: re-logging-out keeps the first end time rather than moving it.
UPDATE sessions
SET ended_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE id = ? AND ended_at IS NULL;

-- name: touch_session_with_skew
UPDATE sessions SET last_seen = strftime('%Y-%m-%dT%H:%M:%fZ','now'), clock_skew_ms = ? WHERE id = ?;

-- name: touch_session
UPDATE sessions SET last_seen = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?;

-- name: record_issued_bundle_version
-- The bundle version the server actually served to this session, recorded
-- at /w/bundle time. w_sync compares against THIS, not against the
-- bundleVersion the phone claims in its sync payload -- the party holding
-- the phones is the party being billed, so a client-supplied number cannot
-- be what decides whether a reject gets verified and charged.
--
-- MAX(): a session can refetch an older cached bundle after a newer one
-- (initBundle applies the cache before the network refresh lands), and the
-- high-water mark is what "this phone has seen version N" means.
UPDATE sessions
SET bundle_version_issued = MAX(bundle_version_issued, ?)
WHERE id = ?;

-- name: line_ids_for_shift
-- Every manifest_line reachable from a shift, with its expected quantity.
-- Used to validate a client-supplied manifestLineId before storing it, and
-- to decide at what count a repeat scan becomes a duplicate -- both per
-- scan, so both are answered from one query per batch rather than one
-- lookup per scan.
SELECT ml.id AS id, ml.qty_expected AS qty_expected
FROM manifest_lines ml
JOIN shift_manifests sm ON sm.manifest_id = ml.manifest_id
WHERE sm.shift_id = ?;

-- name: line_scanned_count_for_shift
-- How many times a manifest line has already been scanned OK across the
-- whole shift, by any worker on any phone. The client-side duplicate check
-- is per page-load and per device; this is the cross-worker one.
SELECT COUNT(*) AS n
FROM scans s
JOIN sessions se ON se.id = s.session_id
WHERE se.shift_id = ?
  AND s.manifest_line_id = ?
  AND s.result = 'ok'
  AND s.uuid <> ?;

-- name: scan_exists
SELECT 1 FROM scans WHERE uuid = ?;

-- name: insert_scan
-- ts_server is COALESCEd rather than left to the column default so a
-- caller (the operator GUI's forensic tooling, tests reconstructing a
-- timeline) can supply an explicit server time, while the normal sync
-- path passes NULL and gets 'now'. ts_client is stored separately and
-- never overwrites it -- clock skew is recorded, not corrected.
INSERT INTO scans (
    uuid, session_id, manifest_line_id, raw_payload, normalized,
    matched_tier, result, decode_ms, match_ms, ts_client,
    ts_server, bundle_version, seq, device_ua
) VALUES (
    ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?,
    COALESCE(?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), ?, ?, ?
);

-- name: get_scan
SELECT * FROM scans WHERE uuid = ?;

-- name: scans_for_line
SELECT * FROM scans WHERE manifest_line_id = ? ORDER BY ts_server ASC;

-- name: count_scans_today
-- Scans within a half-open [start, end) UTC window. The caller computes
-- the window from the account's own timezone (tz.local_day_bounds_utc),
-- so "today" rolls over at the warehouse's midnight rather than UTC's.
-- Comparing ts_server, never ts_client: a phone's clock is not trusted
-- for anything that decides which day a scan lands in.
SELECT COUNT(*) AS n
FROM scans sc
JOIN sessions se ON se.id = sc.session_id
JOIN shifts   sh ON sh.id = se.shift_id
WHERE sh.account_id = ?
  AND sc.ts_server >= ?
  AND sc.ts_server <  ?;

-- name: count_active_shifts
SELECT COUNT(*) AS n FROM shifts WHERE account_id = ? AND revoked_at IS NULL AND token_expires_at > strftime('%Y-%m-%dT%H:%M:%fZ','now');

-- name: count_open_exceptions
-- Dashboard badge. Scoped through the session -> shift chain because
-- exceptions carry no account_id of their own.
SELECT COUNT(*) AS n
FROM exceptions ex
JOIN scans    sc ON sc.uuid = ex.scan_uuid
JOIN sessions se ON se.id = sc.session_id
JOIN shifts   sh ON sh.id = se.shift_id
WHERE sh.account_id = ?
  AND ex.resolved_at IS NULL;

-- name: recent_scans
-- The manifest line label is LEFT JOINed rather than looked up per row.
-- The Live floor view polls this continuously, and the caller used to issue
-- one extra query per scan to fetch sku/description -- 101 queries for 100
-- rows, on a repeating timer.
--
-- The join is also what scopes the label to this account. The standalone
-- lookup was `WHERE id = ?` with no account predicate, so a manifest_line_id
-- belonging to another account (which /w/sync used to accept unvalidated)
-- would have had ITS sku and description rendered on this account's screen.
SELECT sc.*, ml.sku AS line_sku, ml.description AS line_description
FROM scans sc
JOIN sessions se ON se.id = sc.session_id
JOIN shifts sh ON sh.id = se.shift_id
LEFT JOIN manifest_lines ml
       ON ml.id = sc.manifest_line_id
      AND ml.manifest_id IN (SELECT manifest_id FROM shift_manifests WHERE shift_id = sh.id)
WHERE sh.account_id = ?
ORDER BY sc.ts_server DESC
LIMIT ?;

-- name: recent_scans_after
-- Incremental variant: only scans newer than a uuid the caller already has.
-- db.recent_scans accepted an `after_id` argument and then never passed it
-- to the query, so a caller adding incremental polling silently received
-- full results and no error at all.
SELECT sc.*, ml.sku AS line_sku, ml.description AS line_description
FROM scans sc
JOIN sessions se ON se.id = sc.session_id
JOIN shifts sh ON sh.id = se.shift_id
LEFT JOIN manifest_lines ml
       ON ml.id = sc.manifest_line_id
      AND ml.manifest_id IN (SELECT manifest_id FROM shift_manifests WHERE shift_id = sh.id)
WHERE sh.account_id = ?
  AND sc.ts_server > (SELECT ts_server FROM scans WHERE uuid = ?)
ORDER BY sc.ts_server DESC
LIMIT ?;
