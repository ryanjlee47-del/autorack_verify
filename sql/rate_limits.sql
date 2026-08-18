-- Statements for db.py's `rate_limits` section.

-- name: count_rate_limit_events
SELECT COUNT(*) AS n FROM rate_limit_events WHERE bucket = ? AND key = ? AND created_at > ?;

-- name: record_rate_limit_event
INSERT INTO rate_limit_events (bucket, key) VALUES (?,?);

-- name: clear_rate_limit_events
DELETE FROM rate_limit_events WHERE bucket = ? AND key = ?;

-- name: purge_expired_rate_limit_events
DELETE FROM rate_limit_events WHERE created_at <= ?;
