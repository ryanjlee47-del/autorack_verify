-- Statements for db.py's `meta` section.

-- name: applied_versions
SELECT version FROM schema_migrations;

-- name: record_applied_version
INSERT INTO schema_migrations (version) VALUES (?);

-- name: any_account_exists
SELECT 1 FROM accounts LIMIT 1;

-- name: append_only_triggers
SELECT name, sql FROM sqlite_master WHERE type='trigger' AND sql LIKE '%DELETE%';
