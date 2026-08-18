-- Statements for db.py's `billing` section.

-- name: insert_billing_event
INSERT INTO billing_events (account_id, scan_uuid, kind, cents, billable) VALUES (?,?,?,?,?);

-- name: billing_event_exists_for_scan
SELECT 1 FROM billing_events WHERE scan_uuid = ? AND kind = ?;

-- name: count_billable_catches
-- Catches that still stand, i.e. excluding any later reversed.
--
-- billing_events is append-only: a correction is a new 'reversal' row
-- pointing at the same scan, never an edit to the original. Counting
-- catches therefore has to exclude the ones a reversal answers, which
-- is what the NOT EXISTS does. Filtering on billable alone would count
-- refunded catches as revenue.
SELECT COUNT(*) AS n
FROM billing_events be
WHERE be.account_id = ?
  AND be.kind = 'catch'
  AND be.billable = 1
  AND NOT EXISTS (
      SELECT 1 FROM billing_events r
      WHERE r.scan_uuid = be.scan_uuid
        AND r.kind = 'reversal'
  );

-- name: grant_credit
INSERT INTO account_credits (account_id, cents, reason, granted_by) VALUES (?,?,?,?);

-- name: credits_for_account
SELECT * FROM account_credits WHERE account_id = ? ORDER BY created_at DESC;

-- name: billing_events_for_account
-- Optional date range, expressed with nullable bounds rather than by
-- appending clauses in Python. Each bound is bound twice: once to test
-- for NULL (meaning "no bound given") and once to compare. That keeps
-- this a single static statement instead of four assembled variants.
SELECT * FROM billing_events
WHERE account_id = ?
  AND (? IS NULL OR created_at >= ?)
  AND (? IS NULL OR created_at <  ?)
ORDER BY created_at ASC;

-- name: credits_total_for_account
-- Same nullable-bound shape as billing_events_for_account above. The
-- window matters once anything invoices per period: without it, a caller
-- asking for one month's balance would subtract every credit ever
-- granted from that month alone.
SELECT COALESCE(SUM(cents), 0) AS total
FROM account_credits
WHERE account_id = ?
  AND (? IS NULL OR created_at >= ?)
  AND (? IS NULL OR created_at <  ?);

-- name: find_catch_for_scan
SELECT id FROM billing_events WHERE scan_uuid = ? AND kind = 'catch';

-- name: insert_reversal
INSERT INTO billing_events (account_id, scan_uuid, kind, cents, billable, reversal_reason) VALUES (?,?,?,?,?,?);

-- name: get_billing_event
SELECT * FROM billing_events WHERE id = ?;
