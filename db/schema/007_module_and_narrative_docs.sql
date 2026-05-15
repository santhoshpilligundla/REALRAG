-- Allow generated_docs to hold module-level (Pass 2) and narrative-level (Pass 3) docs.
-- Pass 1 = entity-level (entity_id set, file_id null)
-- Pass 2 = module-level (file_id set, entity_id null)
-- Pass 3 = narrative-level (narrative_subject set, entity_id and file_id null)

ALTER TABLE generated_docs ALTER COLUMN entity_id DROP NOT NULL;

ALTER TABLE generated_docs
    ADD COLUMN IF NOT EXISTS file_id UUID REFERENCES repo_files(file_id) ON DELETE CASCADE;

ALTER TABLE generated_docs
    ADD COLUMN IF NOT EXISTS narrative_subject TEXT;

ALTER TABLE generated_docs
    ADD COLUMN IF NOT EXISTS narrative_subject_kind TEXT;  -- 'workflow' | 'cross_repo_chain' | 'concept'

ALTER TABLE generated_docs
    ADD COLUMN IF NOT EXISTS source_entity_ids UUID[];     -- for Pass 2/3 — what we rolled up

-- Drop the old (entity_id, pass_level) unique constraint (auto-named).
DO $$
DECLARE
    cn text;
BEGIN
    FOR cn IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'generated_docs'::regclass AND contype = 'u'
          AND pg_get_constraintdef(oid) ILIKE '%entity_id%pass_level%'
    LOOP
        EXECUTE 'ALTER TABLE generated_docs DROP CONSTRAINT ' || quote_ident(cn);
    END LOOP;
END $$;

-- Partial unique indexes — one row per (anchor, pass_level).
CREATE UNIQUE INDEX IF NOT EXISTS generated_docs_pass1_uq
    ON generated_docs (entity_id, pass_level)
    WHERE pass_level = 'entity';

CREATE UNIQUE INDEX IF NOT EXISTS generated_docs_pass2_uq
    ON generated_docs (file_id, pass_level)
    WHERE pass_level = 'module';

CREATE UNIQUE INDEX IF NOT EXISTS generated_docs_pass3_uq
    ON generated_docs (narrative_subject, pass_level)
    WHERE pass_level = 'narrative';
