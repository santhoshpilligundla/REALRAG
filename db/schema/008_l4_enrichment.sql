-- L4 enrichment columns: statement-level annotation + git history.

ALTER TABLE generated_docs
    ADD COLUMN IF NOT EXISTS statement_annotations TEXT;

ALTER TABLE generated_docs
    ADD COLUMN IF NOT EXISTS git_history JSONB;
