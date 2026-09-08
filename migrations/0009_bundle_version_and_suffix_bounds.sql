-- Two integrity fixes, both of which existed only as assumptions in
-- application code.
--
-- 1. sessions.bundle_version_issued
--
-- w_sync decided whether to verify-and-bill a reject by reading
-- scan.bundleVersion -- a number chosen by the phone:
--
--     if scan.get("bundleVersion", 0) >= shift_now["bundle_version"]:
--         _bill_confirmed_reject(...)
--
-- The party holding the phones is the party being billed, so that guard
-- sat on the wrong side of the trust boundary: a modified client sending
-- bundleVersion: 0 recorded every catch and was charged for none of
-- them. The server now records which bundle version it actually served
-- to a session at /w/bundle time and compares against that instead. The
-- client's claim is still stored on the scan row for forensics; it just
-- no longer decides anything.
--
-- Default 0 is correct for existing rows: they predate any issued
-- bundle, and 0 is what the pre-migration code would have inferred.
ALTER TABLE sessions ADD COLUMN bundle_version_issued INTEGER NOT NULL DEFAULT 0;

-- 2. accounts.loose_suffix_len bounds
--
-- barcode.py clamps with max(suffix_len, MIN_SUFFIX_LEN); barcode.js used
-- `options.suffixLen || DEFAULT_SUFFIX_LEN`, so the value 0 clamped to 6
-- on the server and fell back to 8 on the phone -- two different tier-6
-- keys for the same label, i.e. a silent phantom reject, reachable from
-- the admin surface because act_account_update wrote whatever integer the
-- operator supplied. The JavaScript now uses an explicit null test, and
-- the column gets the bound the code always assumed it had.
--
-- Implemented as triggers rather than a CHECK constraint because SQLite
-- cannot add a CHECK to an existing table without rebuilding it, and a
-- rebuild of `accounts` would have to recreate every foreign key pointing
-- at it. The enforcement is equivalent.
--
-- Existing rows are normalised FIRST. The UPDATE trigger fires on every
-- update to accounts and evaluates NEW.loose_suffix_len, so a row already
-- outside the bound -- which is the entire premise of this fix, since the
-- admin path wrote whatever integer the operator typed -- would abort every
-- future update to that account, including edits to unrelated columns.
-- 8 is DEFAULT_SUFFIX_LEN in both engines.
UPDATE accounts SET loose_suffix_len = 8
WHERE loose_suffix_len IS NULL OR loose_suffix_len < 6 OR loose_suffix_len > 20;

CREATE TRIGGER trg_accounts_loose_suffix_len_insert
BEFORE INSERT ON accounts
FOR EACH ROW WHEN NEW.loose_suffix_len < 6 OR NEW.loose_suffix_len > 20
BEGIN
    SELECT RAISE(ABORT, 'accounts.loose_suffix_len must be between 6 and 20');
END;

CREATE TRIGGER trg_accounts_loose_suffix_len_update
BEFORE UPDATE ON accounts
FOR EACH ROW WHEN NEW.loose_suffix_len < 6 OR NEW.loose_suffix_len > 20
BEGIN
    SELECT RAISE(ABORT, 'accounts.loose_suffix_len must be between 6 and 20');
END;

-- 3. sessions.ended_at
--
-- Logging out flushed the outbox, rendered a summary and navigated to /w.
-- There was no server call at all, so the session token stayed valid: a
-- shared phone, or anyone with the URL in browser history, could reopen a
-- finished worker's scan screen and keep scanning as them.
--
-- Ending a session deliberately does NOT stop /w/sync or /w/appeal. A phone
-- can log out with scans still queued (outbox.js tags every scan with the
-- session it was taken under, precisely for this), and destroying that path
-- would trade a session-hygiene problem for silent data loss. What ending
-- a session revokes is the ability to acquire anything NEW: opening the
-- scan page, and downloading a bundle.
ALTER TABLE sessions ADD COLUMN ended_at TEXT;
