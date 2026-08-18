-- Statements for db.py's `impersonation` section.

-- name: create_impersonation_token
INSERT INTO impersonation_tokens (token, account_id, user_id, expires_at) VALUES (?,?,?,?);

-- name: get_unredeemed_impersonation_token
SELECT * FROM impersonation_tokens WHERE token = ? AND redeemed_at IS NULL AND expires_at > strftime('%Y-%m-%dT%H:%M:%fZ','now');

-- name: mark_impersonation_token_redeemed
UPDATE impersonation_tokens SET redeemed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'), redeemed_ip = ? WHERE token = ?;
