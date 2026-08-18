-- Email verification for self-service signup.
--
-- /signup previously created a live, already-logged-in account the
-- instant the form was submitted, with no proof the address existed.
-- That is fine when accounts are provisioned by hand for one warehouse;
-- it is an open door once the signup page is reachable publicly, since
-- a script can mint unlimited accounts.
--
-- Existing users are backfilled as verified: they were provisioned
-- manually (or by seed.py) before this check existed, and retroactively
-- marking them unverified would lock real people out of working
-- accounts to enforce a rule that did not exist when they signed up.

ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0;
UPDATE users SET email_verified = 1;

CREATE TABLE email_verification_tokens (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    expires_at TEXT NOT NULL,
    used_at    TEXT
);
CREATE INDEX idx_email_verification_tokens_user ON email_verification_tokens(user_id);
