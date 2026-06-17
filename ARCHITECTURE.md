# RealRAG — Architecture & What Was Built

## What is RealRAG?

RealRAG is a Retrieval-Augmented Generation (RAG) system that lets you ask business questions about the RMS codebase and get plain-English answers — no need to read source code.

**Example questions it can answer:**
- "How does renewal pricing work?"
- "What triggers a lease trade-out report?"
- "How is effective rent calculated?"
- "What happens when a property syncs data?"

---

## High-Level Architecture

```
Source Repos (TFS)
       ↓
   [Pipeline]
  Clone → Parse → Doc-Gen → Embed → FAISS Index
       ↓
   [Chat UI]
  Question → Retrieve → LLM → Business-English Answer
```

---

## Pipeline (One-Time Setup Per Repo)

### Step 1 — Clone
`lib/clone.py` clones repos from TFS into `storage/repos/`

### Step 2 — Parse & Chunk
`lib/chunker.py` walks each repo and dispatches to language-specific parsers:

| Parser | Handles |
|--------|---------|
| `parser_java.py` | Java classes, methods, interfaces |
| `parser_typescript.py` | TypeScript/Angular components |
| `parser_sql.py` | Stored procedures, views, tables |
| `parser_xml.py` | Spring beans, config files |
| `parser_ddl.py` | Database schema definitions |
| `parser_jrxml.py` | JasperReports templates |
| `parser_markdown.py` | Documentation files |

All parsers produce standardized `ParsedEntity` records stored in PostgreSQL.

### Step 3 — Doc Generation (Pass 1, 2, 3)
`lib/doc_gen.py` uses Claude to write business-readable documentation for each code entity using a **6-perspective schema**:
1. Structural (what it is)
2. Behavioral (what it does)
3. Business (why it matters)
4. Edge cases
5. Worked examples
6. Cross-references

### Step 4 — Embed
`lib/embedder.py` sends all chunks to OpenAI's `text-embedding-3-large` model.
Results cached on disk (keyed by SHA1) to avoid re-embedding unchanged code.

### Step 5 — FAISS Index
`lib/faiss_store.py` builds per-repo vector indexes stored at `storage/faiss/`.
These are what power the semantic search at query time.

---

## Chat — 4-Tier Query Router

When you ask a question, `lib/chat.py` routes it through tiers in order:

```
Question
   ↓
Tier 1: Fast retrieval + LLM answer
   ↓ (if answer looks weak/unsure)
Tier 2: Deep search (more chunks retrieved)
   ↓ (if still weak)
Tier 3: Agentic — multi-step tool-use investigation
   ↓ (if enabled via "Live Agents" toggle)
Tier 4: Live code agents — headless Claude agents on the real source
```

### Retrieval (lib/retrieval.py)

Every query runs **5 search methods in parallel**:

| Method | How |
|--------|-----|
| Vector search | FAISS semantic similarity |
| Keyword search | Postgres full-text search |
| Symbol exact-match | Direct DB lookup by name |
| Knowledge graph | SQL triples (what writes to what) |
| Document vectors | Generated doc embeddings |

Top 50 hits → re-ranked → top 5 passed to LLM.

### Agent Mode (lib/agent.py)
Multi-step Claude tool-use loop. Claude plans its own investigation using tools: search, symbol lookup, KG query, code grep. Best for complex cross-repo questions.

### Live Agents (lib/agent_flows.py + lib/claude_runner.py)
Routes question to the right repo chain using `data/agent_flows.yaml` dependency graph, then launches headless Claude Code agents on the live source. Returns root cause + data-fix SQL if applicable.

---

## Key Design Decisions

**Business language only** — System prompts strictly forbid code/SQL/class names in answers. Users get plain English, never "see `ReportProcessorServiceImpl.java:237`".

**Product-agnostic routing** — `data/agent_flows.yaml` defines repo dependency graphs as pure data. Adding a new product requires only YAML, no code changes.

**No LLM in parsing** — Parsing, chunking, and knowledge-graph construction are fully deterministic. LLM is only used for doc generation and answering.

**Durable sessions** — `lib/sessions_repo.py` persists chat history in PostgreSQL so conversations survive page reloads and server restarts.

**Query cache** — `lib/query_cache.py` caches exact question→answer pairs so repeated questions return instantly.

---

## Component Map

```
lib/
├── chat.py           ← Main chat orchestrator (4-tier router)
├── retrieval.py      ← Hybrid 5-method search engine
├── chunker.py        ← Parse repos into entities/chunks
├── doc_gen.py        ← LLM doc generation (Pass 1)
├── doc_gen_pass2.py  ← Module-level rollup docs (Pass 2)
├── doc_gen_pass3.py  ← Cross-module narratives (Pass 3)
├── embedder.py       ← OpenAI embeddings with disk cache
├── faiss_store.py    ← FAISS index read/write
├── agent.py          ← Tier-3 agentic tool-use loop
├── agent_flows.py    ← Flow router using YAML dependency graph
├── claude_runner.py  ← Headless Claude Code agent runner
├── kg.py             ← Knowledge-graph SQL triple queries
├── sessions_repo.py  ← Chat session persistence
├── query_cache.py    ← Exact question→answer cache
├── glossary.py       ← Business term glossary bootstrapper
├── business_docs.py  ← Business HTML doc indexer
├── cross_repo.py     ← Cross-repo edge discovery
└── parsers/          ← Language-specific parsers

data/
├── agent_flows.yaml  ← Repo dependency graphs per product
└── glossary_rms.yaml ← RMS business term definitions

frontend/
└── streamlit_app.py  ← Full UI (Chat, Repos, Pipeline, Coverage)

storage/ (gitignored — download from OneDrive)
├── faiss/            ← Vector indexes per repo
├── pg-data/          ← PostgreSQL database
├── embeddings_cache/ ← On-disk embedding cache
└── repos/            ← Cloned source repos
```

---

## UI Tabs

| Tab | Purpose |
|-----|---------|
| **Chat** | Ask questions, get business-English answers |
| **Add Repos** | Register new TFS repos |
| **Registered Repos** | Clone, parse, doc-gen, embed each repo |
| **Coverage** | See indexing coverage per repo |
| **Activity** | Pipeline run history |

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| UI | Streamlit |
| LLM | Claude (Anthropic) — Haiku / Sonnet / Opus |
| Embeddings | OpenAI `text-embedding-3-large` |
| Vector search | FAISS (Facebook AI Similarity Search) |
| Database | PostgreSQL (embedded via pgserver) |
| Parsers | tree-sitter (Java, TypeScript), lxml (XML), sqlglot (SQL) |
| Code agents | Claude Code (headless) |
