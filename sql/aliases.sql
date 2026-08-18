-- Statements for db.py's `aliases` section.

-- name: add_alias
INSERT INTO aliases (account_id, normalized_key, sku, confirmed_by) VALUES (?,?,?,?);

-- name: list_aliases
SELECT * FROM aliases WHERE account_id = ? ORDER BY confirmed_at DESC;
