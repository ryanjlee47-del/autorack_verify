-- Statements for db.py's `audit` section.

-- name: record_audit
INSERT INTO audit_log (actor, action, target_table, target_id, before_json, after_json, ip) VALUES (?,?,?,?,?,?,?);

-- name: list_audit_log
SELECT * FROM audit_log ORDER BY created_at DESC LIMIT ?;

-- name: list_audit_log_for_manifest
-- Every audited action touching one manifest, including edits to its
-- individual lines.
--
-- audit_log deliberately has no foreign keys (it must stay readable
-- after the rows it describes are gone), so a line edit records its
-- parent only inside the before/after JSON blobs. json_extract is how a
-- manifest_lines row is traced back to the manifest it belonged to.
-- Both blobs are checked because a delete has no after_json and an
-- insert has no before_json.
SELECT *
FROM audit_log
WHERE (target_table = 'manifests' AND target_id = ?)
   OR (
        target_table = 'manifest_lines'
        AND (
             json_extract(before_json, '$.manifest_id') = ?
          OR json_extract(after_json,  '$.manifest_id') = ?
        )
   )
ORDER BY created_at DESC;

