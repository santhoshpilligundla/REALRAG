-- Domain glossary: terms with canonical definitions.
-- Per bible §11: "domain glossary expansion" — injected into doc-gen prompts
-- to ground the LLM and prevent hallucinated synonyms.
--
-- Scoping: a glossary entry can be product-scoped (one product) or global (NULL product_id),
-- and optionally repo-scoped (per-repo override).

CREATE TABLE IF NOT EXISTS domain_glossary (
    glossary_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    term         TEXT NOT NULL,
    canonical    TEXT,
    definition   TEXT NOT NULL,
    product_id   UUID REFERENCES products(product_id) ON DELETE CASCADE,
    repo_id      UUID REFERENCES repos(repo_id)        ON DELETE CASCADE,
    notes        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- NULLS NOT DISTINCT (PG 15+) lets us treat NULL product_id / repo_id as equal.
-- This makes the (term, product_id, repo_id) trio uniquely identify an entry
-- even when scope columns are unset.
CREATE UNIQUE INDEX IF NOT EXISTS domain_glossary_scope_uq
    ON domain_glossary (term, product_id, repo_id) NULLS NOT DISTINCT;

CREATE INDEX IF NOT EXISTS domain_glossary_product_idx ON domain_glossary(product_id);
CREATE INDEX IF NOT EXISTS domain_glossary_repo_idx    ON domain_glossary(repo_id);
