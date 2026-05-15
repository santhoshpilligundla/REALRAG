-- RealRAG initial schema.
-- Phase 1: products + repos only. The repos table carries the load-bearing
-- overview fields from RealRAG-bible §7.5; everything else (entities, chunks,
-- generated_docs, examples, cross_repo_edges, audit_log) is added when the
-- ingestion pipeline lands in a later milestone.

CREATE TABLE IF NOT EXISTS products (
    product_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name       TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO products (name) VALUES ('RMS'), ('OneSite'), ('ILM')
ON CONFLICT (name) DO NOTHING;

CREATE TABLE IF NOT EXISTS repos (
    repo_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id UUID NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,

    tfs_url    TEXT NOT NULL,
    branch     TEXT NOT NULL DEFAULT 'master',
    sub_path   TEXT,
    pat_secret TEXT,

    display_name          TEXT NOT NULL,
    one_line_description  TEXT NOT NULL,
    repo_role             TEXT NOT NULL
        CHECK (repo_role IN ('UI','API','ETL','Reports','Config','Other')),
    major_workflows       TEXT[] NOT NULL,
    key_business_concepts TEXT[] NOT NULL,
    critical_entry_points TEXT[] NOT NULL DEFAULT '{}',
    priority              TEXT NOT NULL
        CHECK (priority IN ('P0','P1','P2','P3')),
    languages             TEXT[] NOT NULL DEFAULT '{}',
    owner_team            TEXT NOT NULL,
    owner_contact         TEXT NOT NULL,
    related_repos         UUID[] NOT NULL DEFAULT '{}',
    special_notes         TEXT,

    clone_depth TEXT NOT NULL DEFAULT 'shallow_50'
        CHECK (clone_depth IN ('full','shallow_50','shallow_100')),
    enable_lfs  BOOLEAN NOT NULL DEFAULT false,

    last_indexed_sha TEXT,
    status TEXT NOT NULL DEFAULT 'registered'
        CHECK (status IN ('registered','cloning','parsing','indexing','ready','error','disabled')),

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (product_id, display_name),
    UNIQUE (product_id, tfs_url, branch)
);

CREATE INDEX IF NOT EXISTS repos_product_id_idx ON repos(product_id);
CREATE INDEX IF NOT EXISTS repos_status_idx     ON repos(status);
