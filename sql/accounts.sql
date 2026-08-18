-- Account-level queries: the tenant record itself and its status.
--
-- One account is one warehouse. Every other table in the schema is scoped
-- to an account_id, so these are the queries that decide whose data a
-- request is allowed to touch.
--
-- Loaded by sqlstore.py as "accounts.<name>".

-- name: create_account
INSERT INTO accounts (
    name, plan, price_per_catch_cents, free_allowance,
    loose_match_enabled, loose_suffix_len, worker_self_resolve, timezone
) VALUES (?, ?, ?, ?, ?, ?, ?, ?);

-- name: get_account
SELECT * FROM accounts WHERE id = ?;

-- name: list_accounts
-- Newest first: the operator GUI's Accounts tab is used most often right
-- after provisioning a new account, so the row someone just created
-- should be the one at the top.
SELECT * FROM accounts ORDER BY created_at DESC;

-- name: set_account_status
-- Suspending blocks new shift joins but never rejects an in-flight sync
-- (see app.py's w_sync). Scans are append-only and are not dropped
-- because a bill went unpaid.
UPDATE accounts SET status = ? WHERE id = ?;
