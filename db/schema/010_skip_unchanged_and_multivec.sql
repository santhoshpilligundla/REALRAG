-- Skip-if-unchanged (bible §7.1 step 7) + multi-vector per entity (§11).

-- Track which body the doc was generated against. If the entity is re-parsed
-- and its body_hash matches the doc's source_body_hash, skip re-doc-gen.
ALTER TABLE generated_docs
    ADD COLUMN IF NOT EXISTS source_body_hash TEXT;

-- Multi-vector per entity: signature embed + body embed + summary embed
-- (where summary is the Pass 1 doc text). All three live in code_chunks
-- with different vector_kind values. The retriever can weight per-kind.
ALTER TABLE code_chunks
    ADD COLUMN IF NOT EXISTS vector_kind TEXT DEFAULT 'body';

CREATE INDEX IF NOT EXISTS code_chunks_vector_kind_idx ON code_chunks(vector_kind);
CREATE INDEX IF NOT EXISTS code_chunks_entity_kind_idx ON code_chunks(entity_id, vector_kind);
