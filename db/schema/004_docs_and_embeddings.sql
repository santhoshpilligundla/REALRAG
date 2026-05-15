-- Generated docs (LLM-written) + embedding metadata.

CREATE TABLE IF NOT EXISTS generated_docs (
    doc_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo_id         UUID NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
    entity_id       UUID NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    pass_level      TEXT NOT NULL CHECK (pass_level IN ('entity','module','narrative')),
    depth_tier      TEXT NOT NULL CHECK (depth_tier IN ('L1','L2','L3','L4')),

    structural        TEXT NOT NULL,
    behavioral        TEXT NOT NULL,
    business          TEXT,
    edge_cases        TEXT,
    worked_example    JSONB,
    cross_references  TEXT,

    model_used        TEXT NOT NULL,
    prompt_tokens     INT,
    completion_tokens INT,
    verified          BOOLEAN NOT NULL DEFAULT false,

    embedding_model   TEXT,
    faiss_ord         INTEGER,

    generated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (entity_id, pass_level)
);

CREATE INDEX IF NOT EXISTS generated_docs_repo_id_idx    ON generated_docs(repo_id);
CREATE INDEX IF NOT EXISTS generated_docs_entity_id_idx  ON generated_docs(entity_id);
CREATE INDEX IF NOT EXISTS generated_docs_pass_level_idx ON generated_docs(pass_level);

-- examples table for the bible's L4 "multiple worked examples per rule" requirement.
-- Pass 1 fills one worked_example on the doc; L4 expansion can write extra examples here.
CREATE TABLE IF NOT EXISTS examples (
    example_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id        UUID REFERENCES products(product_id) ON DELETE CASCADE,
    repo_id           UUID NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
    entity_id         UUID REFERENCES entities(entity_id) ON DELETE CASCADE,
    concept           TEXT NOT NULL,
    narrative         TEXT NOT NULL,
    inputs            JSONB,
    outputs           JSONB,
    calculation_steps TEXT,
    cited_tables      TEXT[],
    cited_columns     TEXT[],
    confidence        REAL,
    verified          BOOLEAN NOT NULL DEFAULT false,
    embedding_model   TEXT,
    faiss_ord         INTEGER,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS examples_repo_id_idx   ON examples(repo_id);
CREATE INDEX IF NOT EXISTS examples_entity_id_idx ON examples(entity_id);
