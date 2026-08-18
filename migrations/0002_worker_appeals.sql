-- Worker-initiated appeals: a worker who disagrees with a REJECT can
-- (if the account's worker_self_resolve setting allows it) submit a
-- photo + optional note for the owner to review. This reuses the
-- existing 'worker_reported' exceptions.kind value from the initial
-- schema (it was defined in migration 0001 but never wired up until now).

ALTER TABLE exceptions ADD COLUMN photo_path TEXT;
ALTER TABLE exceptions ADD COLUMN worker_note TEXT;
