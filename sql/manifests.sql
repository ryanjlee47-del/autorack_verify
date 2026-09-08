-- Statements for db.py's `manifests` section.

-- name: create_manifest
INSERT INTO manifests (account_id, ref, source_filename, status) VALUES (?,?,?,'draft');

-- name: get_manifest
SELECT * FROM manifests WHERE id = ?;

-- name: list_manifests
SELECT * FROM manifests WHERE account_id = ? ORDER BY created_at DESC;

-- name: list_committed_manifests
SELECT * FROM manifests WHERE account_id = ? AND status = 'committed' ORDER BY created_at DESC;

-- name: get_manifest_lines_for_account
SELECT ml.* FROM manifest_lines ml JOIN manifests m ON m.id = ml.manifest_id WHERE m.account_id = ? AND m.status = 'committed' ORDER BY ml.id LIMIT ?;

-- name: search_manifest_lines_for_account
-- Type-ahead for the exception resolver's "resolve to line" control.
--
-- ESCAPE '\' on every LIKE is required, not decorative: a search term
-- containing % or _ would otherwise be treated as a wildcard. SKUs
-- routinely contain underscores, so without this, typing a real SKU
-- matches far more than it should. db._escape_like does the escaping.
--
-- Committed manifests only -- a draft has not been verified against
-- anything yet and must not be offered as a resolution target.
SELECT ml.*
FROM manifest_lines ml
JOIN manifests m ON m.id = ml.manifest_id
WHERE m.account_id = ?
  AND m.status = 'committed'
  AND (
        ml.sku         LIKE ? ESCAPE '\'
     OR ml.description LIKE ? ESCAPE '\'
     OR ml.raw_barcode LIKE ? ESCAPE '\'
  )
ORDER BY ml.sku
LIMIT ?;

-- name: insert_manifest_lines
INSERT INTO manifest_lines (manifest_id, line_no, sku, description, qty_expected, raw_barcode) VALUES (?,?,?,?,?,?);

-- name: get_manifest_lines
SELECT * FROM manifest_lines WHERE manifest_id = ? ORDER BY line_no;

-- name: count_manifest_lines
SELECT COUNT(*) AS n FROM manifest_lines WHERE manifest_id = ?;

-- name: page_manifest_lines
SELECT * FROM manifest_lines WHERE manifest_id = ? ORDER BY line_no LIMIT ? OFFSET ?;

-- name: get_manifest_line
SELECT * FROM manifest_lines WHERE id = ?;

-- name: next_manifest_line_no
SELECT COALESCE(MAX(line_no), 0) AS max_no FROM manifest_lines WHERE manifest_id = ?;

-- name: insert_manifest_line
INSERT INTO manifest_lines (manifest_id, line_no, sku, description, qty_expected, raw_barcode) VALUES (?,?,?,?,?,?);

-- name: increment_manifest_line_count
UPDATE manifests SET line_count = line_count + 1 WHERE id = ?;

-- name: update_manifest_line
UPDATE manifest_lines SET sku = ?, description = ?, qty_expected = ?, raw_barcode = ? WHERE id = ?;

-- name: delete_line_keys_for_line
DELETE FROM line_keys WHERE manifest_line_id = ?;

-- name: delete_manifest_line
DELETE FROM manifest_lines WHERE id = ?;

-- name: decrement_manifest_line_count
UPDATE manifests SET line_count = line_count - 1 WHERE id = ?;

-- name: insert_line_keys
INSERT INTO line_keys (manifest_line_id, tier, key, collision) VALUES (?,?,?,?);

-- name: set_manifest_status_and_count
UPDATE manifests SET status = ?, line_count = ? WHERE id = ?;

-- name: set_manifest_status
UPDATE manifests SET status = ? WHERE id = ?;

-- name: bump_manifest_bundle_version
UPDATE manifests SET bundle_version = bundle_version + 1 WHERE id = ?;

-- name: delete_line_keys_for_manifest
DELETE FROM line_keys WHERE manifest_line_id IN (SELECT id FROM manifest_lines WHERE manifest_id = ?);

-- name: get_owned_manifest_line
-- One manifest line, scoped to the account that owns its manifest.
-- Used when resolving an exception to a line: without the account
-- predicate an owner could attach one of another warehouse's lines to
-- their own exception, which then feeds billing.
SELECT ml.*
FROM manifest_lines ml
JOIN manifests m ON m.id = ml.manifest_id
WHERE ml.id = ?
  AND m.account_id = ?;
