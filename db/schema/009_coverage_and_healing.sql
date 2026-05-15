-- Coverage matrix view + self-healing queue.

-- Failed-question queue. Chat code writes a row here when a user query
-- retrieved nothing useful (refusal, no citations, low confidence).
-- Bible §6 strategy 6: feeds the auto-fill job that documents the gap.
CREATE TABLE IF NOT EXISTS failed_questions (
    failed_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id       UUID REFERENCES sessions(session_id) ON DELETE SET NULL,
    user_id          TEXT,
    product_id       UUID REFERENCES products(product_id) ON DELETE SET NULL,
    question         TEXT NOT NULL,
    refusal_reason   TEXT,
    retrieved_count  INTEGER DEFAULT 0,
    used_count       INTEGER DEFAULT 0,
    suspected_entities UUID[],
    addressed_at     TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS failed_questions_addressed_idx ON failed_questions(addressed_at);
CREATE INDEX IF NOT EXISTS failed_questions_product_idx   ON failed_questions(product_id);
CREATE INDEX IF NOT EXISTS failed_questions_created_idx   ON failed_questions(created_at DESC);

-- Per-entity coverage: which doc artefacts exist for this entity.
-- Materialized lazily: callers SELECT FROM the view, no maintenance needed.
CREATE OR REPLACE VIEW entity_coverage AS
SELECT
    e.entity_id,
    e.repo_id,
    e.kind,
    e.qualified_name,
    e.name,
    EXISTS (SELECT 1 FROM generated_docs d
             WHERE d.entity_id = e.entity_id AND d.pass_level = 'entity')
        AS has_pass1,
    EXISTS (SELECT 1 FROM generated_docs d
             WHERE d.entity_id = e.entity_id AND d.pass_level = 'entity'
               AND d.verified IS TRUE)
        AS pass1_verified,
    EXISTS (SELECT 1 FROM generated_docs d
             WHERE d.entity_id = e.entity_id AND d.pass_level = 'entity'
               AND d.statement_annotations IS NOT NULL
               AND d.statement_annotations <> '')
        AS has_statement_annotations,
    EXISTS (SELECT 1 FROM generated_docs d
             WHERE d.entity_id = e.entity_id AND d.pass_level = 'entity'
               AND d.git_history IS NOT NULL)
        AS has_git_history,
    (SELECT COUNT(*) FROM examples ex WHERE ex.entity_id = e.entity_id)
        AS extra_examples_count,
    EXISTS (SELECT 1 FROM generated_docs d
             WHERE d.file_id = e.file_id AND d.pass_level = 'module')
        AS file_has_module_doc,
    (SELECT COUNT(*) FROM cross_repo_edges cre
      WHERE cre.from_entity_id = e.entity_id OR cre.to_entity_id = e.entity_id)
        AS cross_repo_edge_count
FROM entities e;

-- Per-repo summary
CREATE OR REPLACE VIEW repo_coverage_summary AS
SELECT
    r.repo_id,
    r.display_name,
    COUNT(*)                                    AS total_entities,
    COUNT(*) FILTER (WHERE has_pass1)          AS pass1_count,
    COUNT(*) FILTER (WHERE pass1_verified)     AS pass1_verified_count,
    COUNT(*) FILTER (WHERE has_statement_annotations) AS statement_annotated_count,
    COUNT(*) FILTER (WHERE has_git_history)    AS git_history_count,
    COUNT(*) FILTER (WHERE extra_examples_count > 0) AS multi_example_count,
    COUNT(*) FILTER (WHERE file_has_module_doc)      AS module_doc_covered_count
FROM entity_coverage ec
JOIN repos r ON r.repo_id = ec.repo_id
GROUP BY r.repo_id, r.display_name;
