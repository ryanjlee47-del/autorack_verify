-- Statements for db.py's `aliases` section.

-- name: add_alias
INSERT INTO aliases (account_id, normalized_key, sku, confirmed_by) VALUES (?,?,?,?);

-- name: list_aliases
SELECT * FROM aliases WHERE account_id = ? ORDER BY confirmed_at DESC;

-- name: shifts_touching_sku
-- Every non-revoked shift of this account whose manifests contain a line
-- with the given sku. Learning an alias for that sku changes what those
-- shifts' bundles should contain, so their bundle_version has to move --
-- otherwise the phones never refetch and the server's own cached match
-- index (keyed on bundle_version) keeps serving the pre-alias build.
SELECT DISTINCT s.id AS id
FROM shifts s
JOIN shift_manifests sm ON sm.shift_id = s.id
JOIN manifest_lines ml ON ml.manifest_id = sm.manifest_id
WHERE s.account_id = ?
  AND ml.sku = ?
  AND s.revoked_at IS NULL;
