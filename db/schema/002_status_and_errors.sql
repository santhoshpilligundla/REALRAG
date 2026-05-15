-- Widen status enum and capture clone/parse errors per repo.

DO $$
DECLARE
    cn text;
BEGIN
    FOR cn IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'repos'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) ILIKE '%status%'
    LOOP
        EXECUTE 'ALTER TABLE repos DROP CONSTRAINT ' || quote_ident(cn);
    END LOOP;
END $$;

ALTER TABLE repos
    ADD CONSTRAINT repos_status_check CHECK (status IN (
        'registered','cloning','cloned','parsing','chunking',
        'embedding','indexing','ready','error','disabled'
    ));

ALTER TABLE repos ADD COLUMN IF NOT EXISTS error_message TEXT;
ALTER TABLE repos ADD COLUMN IF NOT EXISTS clone_path TEXT;
