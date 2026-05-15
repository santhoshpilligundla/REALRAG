-- Pipeline tables: walked files, extracted entities, code chunks, run history.

CREATE TABLE IF NOT EXISTS repo_files (
    file_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo_id    UUID NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
    path       TEXT NOT NULL,
    language   TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    parsed     BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (repo_id, path)
);

CREATE INDEX IF NOT EXISTS repo_files_repo_id_idx  ON repo_files(repo_id);
CREATE INDEX IF NOT EXISTS repo_files_language_idx ON repo_files(language);

CREATE TABLE IF NOT EXISTS entities (
    entity_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo_id          UUID NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
    file_id          UUID NOT NULL REFERENCES repo_files(file_id) ON DELETE CASCADE,
    parent_entity_id UUID REFERENCES entities(entity_id) ON DELETE CASCADE,
    kind             TEXT NOT NULL,
    name             TEXT NOT NULL,
    qualified_name   TEXT,
    signature        TEXT,
    body_hash        TEXT NOT NULL,
    start_line       INTEGER NOT NULL,
    end_line         INTEGER NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS entities_repo_id_idx ON entities(repo_id);
CREATE INDEX IF NOT EXISTS entities_file_id_idx ON entities(file_id);
CREATE INDEX IF NOT EXISTS entities_kind_idx    ON entities(kind);
CREATE INDEX IF NOT EXISTS entities_qname_idx   ON entities(qualified_name);

CREATE TABLE IF NOT EXISTS code_chunks (
    chunk_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo_id         UUID NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
    file_id         UUID NOT NULL REFERENCES repo_files(file_id) ON DELETE CASCADE,
    entity_id       UUID REFERENCES entities(entity_id) ON DELETE SET NULL,
    content         TEXT NOT NULL,
    language        TEXT NOT NULL,
    start_line      INTEGER NOT NULL,
    end_line        INTEGER NOT NULL,
    sha             TEXT NOT NULL,
    embedding_model TEXT,
    faiss_ord       INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS code_chunks_repo_id_idx   ON code_chunks(repo_id);
CREATE INDEX IF NOT EXISTS code_chunks_file_id_idx   ON code_chunks(file_id);
CREATE INDEX IF NOT EXISTS code_chunks_entity_id_idx ON code_chunks(entity_id);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo_id         UUID REFERENCES repos(repo_id) ON DELETE CASCADE,
    stage           TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('running','success','error','cancelled')),
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    elapsed_seconds REAL,
    counts          JSONB,
    error_message   TEXT,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS pipeline_runs_repo_id_idx    ON pipeline_runs(repo_id);
CREATE INDEX IF NOT EXISTS pipeline_runs_started_at_idx ON pipeline_runs(started_at DESC);
