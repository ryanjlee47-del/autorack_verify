-- Statements for db.py's `workers` section.

-- name: find_worker_by_name
SELECT id FROM workers WHERE account_id = ? AND display_name = ?;

-- name: touch_worker_last_seen
UPDATE workers SET last_seen = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?;

-- name: insert_worker
INSERT INTO workers (account_id, display_name) VALUES (?,?);

-- name: worker_stats_for_account
-- Per-worker scan breakdown plus the dollars attributable to their
-- rejects. A worker with a high reject rate is directly costing the
-- account per-catch fees; that is meant to be visible to the owner, not
-- a hidden surveillance metric (see the /workers page).
--
-- The billed_cents subquery is a separate aggregate joined in, rather
-- than another SUM in the outer query, because the outer query already
-- fans out one row per scan. Summing billing_events across that fan-out
-- would multiply each charge by the worker's scan count. MAX() over the
-- pre-aggregated value is a no-op that survives the join.
--
-- 'reversal' rows carry negative cents, so including both kinds in the
-- subquery nets reversals off automatically.
SELECT
    w.id AS worker_id,
    w.display_name,
    w.active,
    COUNT(sc.uuid) AS total_scans,
    SUM(CASE WHEN sc.result = 'ok'         THEN 1 ELSE 0 END) AS ok_count,
    SUM(CASE WHEN sc.result = 'reject'     THEN 1 ELSE 0 END) AS reject_count,
    SUM(CASE WHEN sc.result = 'duplicate'  THEN 1 ELSE 0 END) AS duplicate_count,
    SUM(CASE WHEN sc.result = 'unresolved' THEN 1 ELSE 0 END) AS unresolved_count,
    COALESCE(MAX(billed.cents), 0) AS billed_cents
FROM workers w
LEFT JOIN sessions se ON se.worker_id = w.id
LEFT JOIN scans    sc ON sc.session_id = se.id
LEFT JOIN (
    SELECT se2.worker_id AS worker_id, SUM(be.cents) AS cents
    FROM billing_events be
    JOIN scans    sc2 ON sc2.uuid = be.scan_uuid
    JOIN sessions se2 ON se2.id = sc2.session_id
    WHERE be.kind IN ('catch', 'reversal')
      AND be.billable = 1
    GROUP BY se2.worker_id
) billed ON billed.worker_id = w.id
WHERE w.account_id = ?
GROUP BY w.id
ORDER BY reject_count DESC;

-- name: worker_session_stats
-- Tally for one shift session, used for the worker's own end-of-shift
-- summary. Counts only, never dollars: the money framing belongs on the
-- owner's Workers page, not on the phone of the person being measured.
SELECT
    COUNT(*) AS total_scans,
    SUM(CASE WHEN result = 'ok'         THEN 1 ELSE 0 END) AS ok_count,
    SUM(CASE WHEN result = 'reject'     THEN 1 ELSE 0 END) AS reject_count,
    SUM(CASE WHEN result = 'duplicate'  THEN 1 ELSE 0 END) AS duplicate_count,
    SUM(CASE WHEN result = 'unresolved' THEN 1 ELSE 0 END) AS unresolved_count
FROM scans
WHERE session_id = ?;

-- name: get_worker
SELECT * FROM workers WHERE id = ?;

-- name: rename_worker
UPDATE workers SET display_name = ? WHERE id = ?;

-- name: set_worker_active
UPDATE workers SET active = ? WHERE id = ?;

-- name: reassign_sessions_to_worker
UPDATE sessions SET worker_id = ? WHERE worker_id = ?;

-- name: delete_worker
DELETE FROM workers WHERE id = ?;
