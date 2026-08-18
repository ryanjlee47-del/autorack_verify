-- Owner/manager logins and their browser sessions.
--
-- These are the humans who log in to the web app. Floor workers are NOT
-- here: a worker has no credential at all and lives in `workers`, keyed
-- only by the name they type at /w/join. See sql/workers.sql.
--
-- Every email is stored and compared lowercased; db.py lowercases on the
-- way in so these statements never need to.
--
-- Loaded by sqlstore.py as "users.<name>".

-- name: create_user
INSERT INTO users (account_id, email, pw_hash, role) VALUES (?, ?, ?, ?);

-- name: get_user
SELECT * FROM users WHERE id = ?;

-- name: get_user_by_email
SELECT * FROM users WHERE email = ?;

-- name: list_users_for_account
SELECT * FROM users WHERE account_id = ? ORDER BY id;

-- name: update_user_password
UPDATE users SET pw_hash = ? WHERE id = ?;

-- name: update_user_role
UPDATE users SET role = ? WHERE id = ?;

-- name: update_user_email
UPDATE users SET email = ? WHERE id = ?;

-- name: count_owners_for_account
-- Guards db.delete_user: an account whose last owner is deleted has
-- nobody left who can log in to it, and deleting the account itself is a
-- separate action that does not exist yet.
SELECT COUNT(*) AS n FROM users WHERE account_id = ? AND role = 'owner';

-- name: delete_user
DELETE FROM users WHERE id = ?;

-- name: delete_web_sessions_for_user
-- Disposable session state, deleted outright when a user is removed.
DELETE FROM web_sessions WHERE user_id = ?;

-- name: detach_impersonation_tokens_for_user
-- Detached rather than deleted: an impersonation token is a historical
-- record of something an operator did, not the user's own data, so it
-- must survive the user it referenced. The column is nullable for
-- exactly this reason.
UPDATE impersonation_tokens SET user_id = NULL WHERE user_id = ?;

-- name: create_web_session
INSERT INTO web_sessions (token, user_id, account_id, expires_at) VALUES (?, ?, ?, ?);

-- name: get_web_session
-- Expiry is enforced in the WHERE clause rather than by a cleanup job, so
-- an expired session is unusable the moment it lapses even if nothing has
-- swept the table.
SELECT * FROM web_sessions
WHERE token = ?
  AND expires_at > strftime('%Y-%m-%dT%H:%M:%fZ', 'now');

-- name: delete_web_session
DELETE FROM web_sessions WHERE token = ?;

-- name: first_user_for_account
-- The account's lowest-id user, used as the impersonation target when an
-- operator opens an account without naming a specific user. Ordered by
-- id rather than role because the first user created is the one the
-- account was provisioned with.
SELECT * FROM users WHERE account_id = ? ORDER BY id LIMIT 1;
