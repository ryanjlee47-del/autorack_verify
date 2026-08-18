-- Remove email verification.
--
-- 0007 added a verification round trip to self-service signup. It was
-- never switched on in production: REQUIRE_EMAIL_VERIFICATION defaulted
-- to False and there is no SMTP integration in this stack, so the
-- "verification email" only ever went to the application log. What it
-- did do was put a permanent "Verify your email address" banner on the
-- dashboard of every account created through /signup, pointing at a
-- resend button that also sent nothing. Removing the feature outright is
-- honest; leaving a dead banner that asks users to complete a step that
-- cannot be completed is not.
--
-- Signup keeps its other protections: the per-IP rate limit
-- (0006_rate_limits.sql, SIGNUP_MAX_PER_IP) and the duplicate-address
-- check both still apply.

DROP INDEX IF EXISTS idx_email_verification_tokens_user;
DROP TABLE IF EXISTS email_verification_tokens;

-- No index or constraint referenced this column, so a plain DROP COLUMN
-- is safe here (SQLite 3.35+; Ubuntu 22.04, which PythonAnywhere runs,
-- ships 3.37).
ALTER TABLE users DROP COLUMN email_verified;
