-- Lets an owner mark a worker who's left as inactive (informational --
-- it does not block rejoining under the same name, since there is no
-- worker authentication by design; see db.merge_workers for correcting
-- name-typo duplicates like "Bob" vs "bob").
ALTER TABLE workers ADD COLUMN active INTEGER NOT NULL DEFAULT 1;
