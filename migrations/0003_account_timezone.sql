-- Display-only timezone preference per account. Storage stays UTC
-- everywhere (see tz.py) -- this only affects what a human reads on the
-- owner web app.
ALTER TABLE accounts ADD COLUMN timezone TEXT NOT NULL DEFAULT 'UTC';
