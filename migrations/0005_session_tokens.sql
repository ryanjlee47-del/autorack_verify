-- Worker `sessions` were identified over HTTP by a bare sequential
-- integer (sid=1, sid=2, ...) with nothing else checked, so anyone who
-- guessed or incremented that id could act as a different worker's scan
-- session -- read their manifest bundle, push fake scans, or upload an
-- appeal photo in their name. Give sessions the same opaque-token
-- treatment shifts already have (shifts.token) so the client-facing
-- identifier can't be enumerated.

ALTER TABLE sessions ADD COLUMN token TEXT;
UPDATE sessions SET token = lower(hex(randomblob(16))) WHERE token IS NULL;
CREATE UNIQUE INDEX idx_sessions_token ON sessions(token);
