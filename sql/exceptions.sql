-- Statements for db.py's `exceptions` section.

-- name: create_exception
INSERT INTO exceptions (scan_uuid, kind, manifest_line_id) VALUES (?,?,?);

-- name: find_existing_worker_appeal
SELECT id FROM exceptions WHERE scan_uuid = ? AND kind = 'worker_reported';

-- name: insert_worker_appeal
INSERT INTO exceptions (scan_uuid, kind, photo_path, worker_note) VALUES (?,'worker_reported',?,?);

-- name: resolve_exception
-- manifest_line_id is COALESCEd so resolving without picking a line
-- (confirming a genuine wrong item, say) leaves any line the server
-- already suggested in place instead of blanking it.
--
-- The audit row for this edit is written BEFORE the transaction that
-- runs it, on its own commit -- see app.py's exception_resolve. A
-- caught mistake must stay reconstructable even if the mutation below
-- then fails.
UPDATE exceptions
SET resolved_by = ?,
    resolved_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    resolution_note = ?,
    manifest_line_id = COALESCE(?, manifest_line_id)
WHERE id = ?;

-- name: list_open_exceptions
-- Everything awaiting owner review on one account, with the scan that
-- raised it and the worker who made it.
--
-- The manifest_lines join is LEFT because only some exceptions carry a
-- suggested line: the server attaches one when it disagreed with a
-- client's 'reject' claim and found a match itself (see app.py's
-- _bill_confirmed_reject). Those become the one-click "Suggested: ..."
-- button, so the owner does not re-search for what the system already
-- found. The other kinds have nothing to suggest and must still appear.
--
-- Scoping is via shifts.account_id, not exceptions: an exception has no
-- account column of its own, and reaching it through the scan's session
-- is what makes cross-account access impossible here.
SELECT
    ex.*,
    sc.raw_payload,
    sc.normalized,
    sc.ts_server,
    w.display_name   AS worker_name,
    sml.sku          AS suggested_sku,
    sml.description  AS suggested_description,
    sml.raw_barcode  AS suggested_raw_barcode
FROM exceptions ex
JOIN scans    sc ON sc.uuid = ex.scan_uuid
JOIN sessions se ON se.id = sc.session_id
JOIN shifts   sh ON sh.id = se.shift_id
JOIN workers  w  ON w.id = se.worker_id
LEFT JOIN manifest_lines sml ON sml.id = ex.manifest_line_id
WHERE sh.account_id = ?
  AND ex.resolved_at IS NULL
ORDER BY ex.created_at DESC;

-- name: get_exception_for_scan
SELECT * FROM exceptions WHERE scan_uuid = ? ORDER BY id DESC LIMIT 1;

-- name: get_owned_exception
-- Fetch one exception, but only if it belongs to the calling account.
--
-- The account scope is the whole point: `exceptions` has no account_id
-- column, so an id alone proves nothing about ownership. Reaching the
-- account through scan -> session -> shift in the WHERE clause is what
-- makes "resolve exception 41" fail for someone else's exception 41
-- rather than succeed. Enforced here rather than by a check after the
-- fetch, so there is no path that loads the row first and forgets to
-- compare afterwards.
SELECT ex.*
FROM exceptions ex
JOIN scans    sc ON sc.uuid = ex.scan_uuid
JOIN sessions se ON se.id = sc.session_id
JOIN shifts   sh ON sh.id = se.shift_id
WHERE ex.id = ?
  AND sh.account_id = ?;

-- name: get_owned_exception_photo
-- Same ownership join as get_owned_exception, for serving a worker's
-- appeal photo. Appeal photos are pictures taken inside someone's
-- warehouse; the path is only ever returned to the account that owns
-- the shift the scan was made on.
SELECT ex.photo_path
FROM exceptions ex
JOIN scans    sc ON sc.uuid = ex.scan_uuid
JOIN sessions se ON se.id = sc.session_id
JOIN shifts   sh ON sh.id = se.shift_id
WHERE ex.id = ?
  AND sh.account_id = ?;
