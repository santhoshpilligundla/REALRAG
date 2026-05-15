-- Knowledge graph (facts), inter-entity dependencies, cross-repo edges,
-- chat sessions/messages, audit log, and prose doc chunks.
-- All from bible §8 data model.

-- Prose doc chunks (markdown / docs / READMEs). Separate from code_chunks
-- so the retriever can route doc-only vs code-only queries when useful.
CREATE TABLE IF NOT EXISTS doc_chunks (
    chunk_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo_id         UUID NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
    file_id         UUID NOT NULL REFERENCES repo_files(file_id) ON DELETE CASCADE,
    entity_id       UUID REFERENCES entities(entity_id) ON DELETE SET NULL,
    source_path     TEXT NOT NULL,
    section         TEXT,
    content         TEXT NOT NULL,
    start_line      INTEGER NOT NULL,
    end_line        INTEGER NOT NULL,
    sha             TEXT NOT NULL,
    embedding_model TEXT,
    faiss_ord       INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS doc_chunks_repo_idx ON doc_chunks(repo_id);
CREATE INDEX IF NOT EXISTS doc_chunks_file_idx ON doc_chunks(file_id);

-- Knowledge-graph triples: subject-predicate-object. Bible §8 + §11
-- "knowledge_graph_triples — some questions answered by SQL on triples,
-- provably 100% accurate, no LLM."
CREATE TABLE IF NOT EXISTS facts (
    fact_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo_id     UUID NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
    entity_id   UUID REFERENCES entities(entity_id) ON DELETE CASCADE,
    subject     TEXT NOT NULL,
    predicate   TEXT NOT NULL,
    object      TEXT NOT NULL,
    confidence  REAL NOT NULL DEFAULT 1.0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS facts_repo_idx       ON facts(repo_id);
CREATE INDEX IF NOT EXISTS facts_subject_idx    ON facts(subject);
CREATE INDEX IF NOT EXISTS facts_predicate_idx  ON facts(predicate);
CREATE INDEX IF NOT EXISTS facts_object_idx     ON facts(object);

-- Same-repo dependency edges (extends/implements/calls/reads/writes).
CREATE TABLE IF NOT EXISTS dependencies (
    from_entity_id UUID NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    to_entity_id   UUID NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    kind           TEXT NOT NULL,
    confidence     REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (from_entity_id, to_entity_id, kind)
);
CREATE INDEX IF NOT EXISTS dependencies_from_idx ON dependencies(from_entity_id);
CREATE INDEX IF NOT EXISTS dependencies_to_idx   ON dependencies(to_entity_id);

-- Cross-repo edges: rm-web → ys → po, etc. Populated from the pattern catalog
-- in Phase 3. Recursive CTEs walk these for trace queries.
CREATE TABLE IF NOT EXISTS cross_repo_edges (
    edge_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_entity_id   UUID REFERENCES entities(entity_id) ON DELETE CASCADE,
    to_entity_id     UUID REFERENCES entities(entity_id) ON DELETE CASCADE,
    from_repo_id     UUID REFERENCES repos(repo_id)        ON DELETE CASCADE,
    to_repo_id       UUID REFERENCES repos(repo_id)        ON DELETE CASCADE,
    kind             TEXT NOT NULL,
    confidence       REAL NOT NULL DEFAULT 1.0,
    discovered_via   TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cre_from_entity_idx ON cross_repo_edges(from_entity_id);
CREATE INDEX IF NOT EXISTS cre_to_entity_idx   ON cross_repo_edges(to_entity_id);
CREATE INDEX IF NOT EXISTS cre_from_repo_idx   ON cross_repo_edges(from_repo_id);
CREATE INDEX IF NOT EXISTS cre_to_repo_idx     ON cross_repo_edges(to_repo_id);
CREATE INDEX IF NOT EXISTS cre_kind_idx        ON cross_repo_edges(kind);

-- Chat sessions + messages. Bible §8.
CREATE TABLE IF NOT EXISTS sessions (
    session_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     TEXT,
    product_id  UUID REFERENCES products(product_id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sessions_user_idx     ON sessions(user_id);
CREATE INDEX IF NOT EXISTS sessions_product_idx  ON sessions(product_id);

CREATE TABLE IF NOT EXISTS messages (
    message_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  UUID REFERENCES sessions(session_id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
    content     TEXT NOT NULL,
    citations   JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS messages_session_idx ON messages(session_id);

-- Audit log: every action (login, register, doc-gen, query) recorded.
CREATE TABLE IF NOT EXISTS audit_log (
    audit_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     TEXT,
    product_id  UUID REFERENCES products(product_id) ON DELETE SET NULL,
    action      TEXT NOT NULL,
    payload     JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_log_user_idx     ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS audit_log_action_idx   ON audit_log(action);
CREATE INDEX IF NOT EXISTS audit_log_created_idx  ON audit_log(created_at DESC);
