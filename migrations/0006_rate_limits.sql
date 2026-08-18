-- Rate limiting moved out of per-process memory and into the database.
--
-- The login throttle previously lived in a Python dict on the Flask app
-- object. That has two failure modes the README already called out as
-- blockers for public exposure: it resets to zero on every restart (so a
-- restart loop clears an attacker's budget), and under the documented
-- `gunicorn --workers 4` deployment each worker keeps its own dict, so
-- the real attempt budget is silently ~N times the configured one.
--
-- One row per (bucket, key, event). Rows are append-only in practice --
-- counting is a windowed COUNT(*) rather than a mutable counter, so two
-- workers recording a hit concurrently can't lose an increment to a
-- read-modify-write race the way a shared counter column could.
--
--   bucket: which limiter ('login', 'signup') -- keeps the namespaces
--           separate so a login lockout can't consume a signup budget.
--   key:    what's being limited. 'login' uses "email|ip" so one abusive
--           IP can't lock a real user out of their own account; 'signup'
--           uses the IP alone, since there's no prior identity to key on.

CREATE TABLE rate_limit_events (
    id          INTEGER PRIMARY KEY,
    bucket      TEXT NOT NULL,
    key         TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Every read is "count rows in this bucket+key newer than T", and the
-- cleanup sweep is "delete rows older than T" -- both served by this.
CREATE INDEX idx_rate_limit_events_lookup ON rate_limit_events(bucket, key, created_at);
