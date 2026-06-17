"""Hybrid retrieval — vector + keyword + symbol-exact + knowledge-graph.

Per bible §7.2:
  - Embed query
  - Parallel retrieval (FAISS vector + Postgres FTS keyword + symbol exact-match
    + graph + doc-vec + cross-repo expansion)
  - Re-rank top-50 → top-5 (deferred — uses score floor + dedup for now)

Returns ranked Hit records with citations attached.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

import numpy as np
from psycopg.rows import dict_row

from uuid import UUID as _UUID

from lib.business_docs import search_business_docs
from lib.db import get_conn
from lib.embedder import embed_texts
from lib.faiss_store import search as faiss_search
from lib.kg import find_entity_by_name
from lib.llm import call_json

_BIZ_REPO_ID = _UUID(int=0)  # sentinel repo for product-agnostic business docs


@dataclass
class Hit:
    chunk_id: UUID | None
    entity_id: UUID | None
    repo_id: UUID
    repo_name: str
    file_path: str | None
    qualified_name: str | None
    kind: str | None
    content: str
    citations: list[dict]   # [{file, start_line, end_line, qname}]
    score: float
    source: str             # 'vector', 'doc_vec', 'fts', 'symbol', 'kg'
    pass_level: str         # 'entity' | 'module' | 'narrative' | 'raw_chunk'


def _hit_from_chunk_row(row: dict, score: float, source: str) -> Hit:
    return Hit(
        chunk_id=row.get("chunk_id"),
        entity_id=row.get("entity_id"),
        repo_id=row["repo_id"],
        repo_name=row.get("repo_name") or "",
        file_path=row.get("file_path"),
        qualified_name=row.get("qualified_name"),
        kind=row.get("kind"),
        content=row.get("content") or "",
        citations=[{
            "file": row.get("file_path") or "",
            "start_line": row.get("start_line"),
            "end_line": row.get("end_line"),
            "qname": row.get("qualified_name") or "",
            "repo": row.get("repo_name") or "",
        }],
        score=score,
        source=source,
        pass_level="raw_chunk",
    )


def _hit_from_doc_row(row: dict, score: float, source: str, pass_level: str) -> Hit:
    parts = []
    if row.get("structural"):
        parts.append("STRUCTURAL: " + row["structural"])
    if row.get("behavioral"):
        parts.append("BEHAVIORAL: " + row["behavioral"])
    if row.get("business"):
        parts.append("BUSINESS: " + row["business"])
    if row.get("cross_references"):
        parts.append("CROSS REFS: " + row["cross_references"])
    return Hit(
        chunk_id=None,
        entity_id=row.get("entity_id"),
        repo_id=row["repo_id"],
        repo_name=row.get("repo_name") or "",
        file_path=row.get("file_path"),
        qualified_name=row.get("qualified_name") or row.get("narrative_subject"),
        kind=row.get("kind") or pass_level,
        content="\n\n".join(parts),
        citations=[{
            "file": row.get("file_path") or "",
            "start_line": row.get("start_line"),
            "end_line": row.get("end_line"),
            "qname": row.get("qualified_name") or row.get("narrative_subject") or "",
            "repo": row.get("repo_name") or "",
            "doc_id": row.get("doc_id"),
        }],
        score=score,
        source=source,
        pass_level=pass_level,
    )


# ---------------------------------------------------------------------------
# Individual retrieval arms
# ---------------------------------------------------------------------------


def _vector_search_repos(
    query_vec: np.ndarray,
    repo_ids: list[UUID],
    kind: str,
    top_k: int,
) -> list[tuple[UUID, float]]:
    out: list[tuple[UUID, float]] = []
    for rid in repo_ids:
        out.extend(faiss_search(rid, kind, query_vec, top_k))
    out.sort(key=lambda x: -x[1])
    return out[:top_k]


def _fetch_chunks_by_ids(chunk_ids: list[UUID]) -> dict[UUID, dict]:
    if not chunk_ids:
        return {}
    sql = """
        SELECT cc.chunk_id, cc.entity_id, cc.repo_id, cc.content, cc.start_line, cc.end_line,
               cc.vector_kind, e.qualified_name, e.kind, f.path AS file_path,
               r.display_name AS repo_name
          FROM code_chunks cc
          LEFT JOIN entities e ON e.entity_id = cc.entity_id
          LEFT JOIN repo_files f ON f.file_id = cc.file_id
          JOIN repos r ON r.repo_id = cc.repo_id
         WHERE cc.chunk_id = ANY(%s)
    """
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, (chunk_ids,))
        return {r["chunk_id"]: r for r in cur.fetchall()}


def _fetch_docs_by_ids(doc_ids: list[UUID]) -> dict[UUID, dict]:
    if not doc_ids:
        return {}
    sql = """
        SELECT d.doc_id, d.entity_id, d.repo_id, d.pass_level,
               d.structural, d.behavioral, d.business, d.cross_references,
               d.narrative_subject,
               e.qualified_name, e.kind, e.start_line, e.end_line,
               f.path AS file_path,
               r.display_name AS repo_name
          FROM generated_docs d
          LEFT JOIN entities e ON e.entity_id = d.entity_id
          LEFT JOIN repo_files f ON f.file_id = COALESCE(d.file_id, e.file_id)
          JOIN repos r ON r.repo_id = d.repo_id
         WHERE d.doc_id = ANY(%s)
    """
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, (doc_ids,))
        return {r["doc_id"]: r for r in cur.fetchall()}


def vector_arm_code(query_vec: np.ndarray, repo_ids: list[UUID], top_k: int = 10) -> list[Hit]:
    pairs = _vector_search_repos(query_vec, repo_ids, "code", top_k)
    chunks = _fetch_chunks_by_ids([cid for cid, _ in pairs])
    out: list[Hit] = []
    for cid, score in pairs:
        row = chunks.get(cid)
        if not row:
            continue
        out.append(_hit_from_chunk_row(row, score, source="vector"))
    return out


def vector_arm_generated(query_vec: np.ndarray, repo_ids: list[UUID], top_k: int = 10) -> list[Hit]:
    pairs = _vector_search_repos(query_vec, repo_ids, "generated", top_k)
    docs = _fetch_docs_by_ids([cid for cid, _ in pairs])
    out: list[Hit] = []
    for cid, score in pairs:
        row = docs.get(cid)
        if not row:
            continue
        out.append(_hit_from_doc_row(row, score, source="doc_vec", pass_level=row["pass_level"]))
    return out


def fts_arm(query: str, repo_ids: list[UUID], top_k: int = 10) -> list[Hit]:
    """Postgres full-text search over code_chunks.content."""
    if not repo_ids or not query.strip():
        return []
    sql = """
        SELECT cc.chunk_id, cc.entity_id, cc.repo_id, cc.content,
               cc.start_line, cc.end_line,
               e.qualified_name, e.kind, f.path AS file_path,
               r.display_name AS repo_name,
               ts_rank(to_tsvector('english', cc.content),
                       plainto_tsquery('english', %s)) AS score
          FROM code_chunks cc
          LEFT JOIN entities e ON e.entity_id = cc.entity_id
          LEFT JOIN repo_files f ON f.file_id = cc.file_id
          JOIN repos r ON r.repo_id = cc.repo_id
         WHERE cc.repo_id = ANY(%s)
           AND to_tsvector('english', cc.content) @@ plainto_tsquery('english', %s)
         ORDER BY score DESC
         LIMIT %s
    """
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, (query, list(repo_ids), query, top_k))
        rows = cur.fetchall()
    return [_hit_from_chunk_row(r, float(r.get("score") or 0.0), source="fts") for r in rows]


def _fetch_entity_docs(entity_ids: list[UUID]) -> dict[UUID, dict]:
    if not entity_ids:
        return {}
    sql = """
        SELECT entity_id, structural, behavioral, business, cross_references
          FROM generated_docs
         WHERE entity_id = ANY(%s) AND pass_level = 'entity'
    """
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, (entity_ids,))
        return {r["entity_id"]: r for r in cur.fetchall()}


def symbol_arm(structural_targets: list[str], product_id: UUID | None = None) -> list[Hit]:
    # Resolve named symbols to entities (exact match), keeping the first hit per entity.
    rows_by_eid: dict[UUID, dict] = {}
    for target in structural_targets:
        for row in find_entity_by_name(target, product_id=product_id, limit=3):
            eid = row.get("entity_id")
            if eid and eid not in rows_by_eid:
                rows_by_eid[eid] = row
    if not rows_by_eid:
        return []

    # Pull the real generated doc for each matched entity so a named-concept
    # question ("how is Leases Needed calculated") gets the actual content — not
    # just a "class X in file Y" pointer — even without the re-ranker.
    docs = _fetch_entity_docs(list(rows_by_eid.keys()))
    out: list[Hit] = []
    for eid, row in rows_by_eid.items():
        d = docs.get(eid)
        if d:
            parts = []
            if d.get("structural"):
                parts.append("STRUCTURAL: " + d["structural"])
            if d.get("behavioral"):
                parts.append("BEHAVIORAL: " + d["behavioral"])
            if d.get("business"):
                parts.append("BUSINESS: " + d["business"])
            if d.get("cross_references"):
                parts.append("CROSS REFS: " + d["cross_references"])
            content, pass_level = "\n\n".join(parts), "entity"
        else:
            content = f"{row.get('kind')} {row.get('qualified_name')} (in {row.get('file_path')})"
            pass_level = "raw_chunk"
        out.append(Hit(
            chunk_id=None, entity_id=eid, repo_id=row["repo_id"],
            repo_name=row.get("repo_name") or "", file_path=row.get("file_path"),
            qualified_name=row.get("qualified_name"), kind=row.get("kind"),
            content=content,
            citations=[{
                "file": row.get("file_path") or "",
                "qname": row.get("qualified_name") or "",
                "repo": row.get("repo_name") or "",
            }],
            score=1.0, source="symbol", pass_level=pass_level,
        ))
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def business_arm(query_vec: np.ndarray, top_k: int = 5) -> list[Hit]:
    """Curated business-doc arm (NLW-style): human-written business prose."""
    out: list[Hit] = []
    for r in search_business_docs(query_vec, top_k):
        out.append(Hit(
            chunk_id=None, entity_id=None, repo_id=_BIZ_REPO_ID,
            repo_name="business-docs", file_path=r.get("source"),
            qualified_name=r.get("title"), kind="business_doc",
            content=r.get("content") or "",
            citations=[{"file": r.get("source") or "", "qname": r.get("title") or "",
                        "repo": "business-docs"}],
            score=float(r.get("score") or 0.0), source="business_doc",
            pass_level="narrative",   # gets the curated-doc boost
        ))
    return out


# Reciprocal-rank-fusion constant (standard k=60). RRF is scale-free, so it
# fuses arms whose raw scores live on different scales (cosine vs ts_rank vs
# symbol) without a shared floor — the old single 0.40 floor silently killed
# whole arms (FTS) and starved good cosine hits.
_RRF_K = 60

# Curated generated docs (3-pass) are the most valuable context; nudge them
# above raw code chunks so the answerer sees narratives, not just source.
_DOC_BOOST = {"narrative": 1.40, "module": 1.25, "entity": 1.15}


def _doc_rank(h: Hit) -> int:
    order = {"narrative": 3, "module": 2, "entity": 1, "raw_chunk": 0}
    return order.get(h.pass_level, 0)


def _dedup_key(h: Hit):
    return (h.entity_id, h.pass_level) if h.entity_id else (h.qualified_name or h.content[:48], h.source)


def _calc_weight(h: Hit) -> float:
    """Bias toward where calculations actually live: the po ETL/SQL engine and
    SQL/query/ETL catalog files — and away from UI components that merely display
    the value. Applied only for calculation questions.
    """
    fp = (h.file_path or "").lower()
    w = 1.0
    if h.repo_name == "po":
        w *= 1.6                      # po = ETL + forecasting/rates calc engine
    if fp.endswith(".sql") or any(k in fp for k in ("queries", "chartsql", "etl", "fcst", "sql.xml")):
        w *= 1.35                     # raw SQL / query catalogs carry the formula
    if ".component.ts" in fp or h.repo_name == "rm-web":
        w *= 0.55                     # UI widgets show the number, don't compute it
    return w


def _critical_boost(h: Hit, critical_paths: set[str]) -> float:
    """Boost hits in repo-curated critical_entry_points files (NLW-style curation:
    the hand-named key files are where the important logic lives)."""
    if not critical_paths:
        return 1.0
    fp = (h.file_path or "").lower()
    return 1.5 if any(cp in fp for cp in critical_paths) else 1.0


def _fuse_and_rank(arms: dict[str, list[Hit]], top_k: int = 8, prefer_calc: bool = False,
                   critical_paths: set[str] | None = None) -> list[Hit]:
    """Reciprocal Rank Fusion across arms + generated-doc boost, dedup-by-entity.

    Each arm is a list already sorted best-first. A document's fused score is the
    sum of 1/(K + rank) over the arms it appears in (agreement across arms wins),
    then multiplied by a small boost for curated doc levels. When `prefer_calc`,
    also bias toward calculation-bearing sources (po / SQL) over UI components.
    """
    agg: dict[object, list] = {}  # key -> [Hit, fused_score]
    for hits in arms.values():
        for rank, h in enumerate(hits):
            key = _dedup_key(h)
            contrib = 1.0 / (_RRF_K + rank + 1)
            if key in agg:
                agg[key][1] += contrib
                # Keep the richer representation (prefer narrative/module/entity doc over raw chunk).
                if _doc_rank(h) > _doc_rank(agg[key][0]):
                    agg[key][0] = h
            else:
                agg[key] = [h, contrib]

    ranked: list[Hit] = []
    for h, score in agg.values():
        h.score = score * _DOC_BOOST.get(h.pass_level, 1.0)
        if prefer_calc:
            h.score *= _calc_weight(h)
        if critical_paths:
            h.score *= _critical_boost(h, critical_paths)
        ranked.append(h)
    ranked.sort(key=lambda x: -x.score)
    return ranked[:top_k]


_RERANK_SYS = """You are a retrieval re-ranker for a code-understanding system. You are given a user question and a numbered list of candidate excerpts (each is either source code or generated documentation from the codebase).

Pick the candidates that would actually help answer the question, best first. IMPORTANT: include a candidate if it is genuinely relevant EVEN IF it uses different terminology than the question (e.g., the question says "model threshold" but the relevant code calls it "exposure / fallback threshold"). Reason about what the question means, not just word overlap. Prefer the candidates that contain the actual logic/answer over ones that merely mention the topic.

Output JSON only: {"ranked": [list of candidate indices, most relevant first]}"""


def _llm_rerank(query: str, hits: list[Hit], top_k: int) -> list[Hit]:
    """Read the candidate pool and reorder by true relevance (bridges vocabulary gaps).

    Falls back to the incoming (RRF) order if the LLM call fails.
    """
    if len(hits) <= top_k:
        return hits[:top_k]
    lines = []
    for i, h in enumerate(hits):
        label = f"{h.repo_name}:{h.qualified_name or '?'} ({h.pass_level})"
        snippet = " ".join((h.content or "").split())[:180]
        lines.append(f"[{i}] {label}\n{snippet}")
    user = (
        f"QUESTION: {query}\n\n"
        f"CANDIDATES:\n" + "\n\n".join(lines) +
        f"\n\nReturn the {top_k} most relevant candidate indices, best first. JSON only."
    )
    try:
        raw, _ = call_json(_RERANK_SYS, user, tier="default", max_tokens=300)
        order = raw.get("ranked") or []
    except Exception:
        return hits[:top_k]

    seen: set[int] = set()
    out: list[Hit] = []
    for idx in order:
        if isinstance(idx, int) and 0 <= idx < len(hits) and idx not in seen:
            out.append(hits[idx])
            seen.add(idx)
        if len(out) >= top_k:
            break
    # Backfill from RRF order if the model returned fewer than top_k.
    for i, h in enumerate(hits):
        if len(out) >= top_k:
            break
        if i not in seen:
            out.append(h)
    return out[:top_k]


# Light relevance gate on the vector arms only (cosine). Drops true noise
# without the old 0.40 floor that starved relevant hits; FTS/symbol are ranked
# by RRF, not by this gate.
_VECTOR_MIN_COS = 0.30


def _symbolic_targets(targets: list[str]) -> list[str]:
    """Keep only identifier-like targets for the symbol arm.

    The intent classifier often emits generic English words ("forecasting",
    "rates", "generation"); matching those by name surfaced junk like
    Constants.forecasting at score 1.0. Real symbols are CamelCase / snake_case
    / qualified / contain a digit. Plain lowercase words are dropped here — they
    are still covered by the vector and FTS arms.
    """
    import re
    out: list[str] = []
    for t in targets:
        t = (t or "").strip()
        if len(t) < 3:
            continue
        if re.search(r"[A-Z]", t) or "_" in t or "." in t or re.search(r"\d", t):
            out.append(t)
    return out


def retrieve(
    *,
    query: str,
    rewrite: str,
    hyde: str | None,
    structural_targets: list[str],
    repo_ids: list[UUID],
    product_id: UUID | None,
    top_k: int = 8,
    rerank: bool = True,
    query_vec=None,
    prefer_calc: bool = False,
    critical_paths: set[str] | None = None,
) -> list[Hit]:
    """Run all arms in parallel, fuse with RRF (+doc boost) into a wide candidate
    pool, then LLM-re-rank that pool down to top_k. Wider pull + re-rank fixes the
    'the right chunk exists but didn't make the top 8' recall problem.

    If `query_vec` is provided, the query embedding step is skipped (the caller
    already embedded it, e.g. in parallel with intent classification for speed).
    """
    sym_targets = _symbolic_targets(structural_targets)
    arm_k = 15  # pull per arm; enough for vocabulary-mismatched hits to enter the pool

    async def _runner() -> dict[str, list[Hit]]:
        loop = asyncio.get_event_loop()

        fts_task = loop.run_in_executor(None, fts_arm, rewrite, repo_ids, arm_k)
        sym_task = loop.run_in_executor(None, symbol_arm, sym_targets, product_id)

        if query_vec is not None:
            qvec = query_vec
            fts_hits, sym_hits = await asyncio.gather(fts_task, sym_task)
        else:
            embed_text = rewrite + ("\n\n" + hyde if hyde else "")
            embed_task = loop.run_in_executor(None, embed_texts, [embed_text])
            (matrix, _model), fts_hits, sym_hits = await asyncio.gather(
                embed_task, fts_task, sym_task)
            if matrix.size == 0:
                return {"fts": list(fts_hits), "symbol": list(sym_hits)}
            qvec = matrix[0]

        arms: dict[str, list[Hit]] = {"fts": list(fts_hits), "symbol": list(sym_hits)}
        code_task = loop.run_in_executor(None, vector_arm_code, qvec, repo_ids, arm_k)
        gen_task  = loop.run_in_executor(None, vector_arm_generated, qvec, repo_ids, arm_k)
        biz_task  = loop.run_in_executor(None, business_arm, qvec, 5)
        code_hits, gen_hits, biz_hits = await asyncio.gather(code_task, gen_task, biz_task)

        arms["vector_code"] = [h for h in code_hits if h.score >= _VECTOR_MIN_COS]
        arms["vector_gen"] = [h for h in gen_hits if h.score >= _VECTOR_MIN_COS]
        arms["business"] = list(biz_hits)
        return arms

    try:
        arms = asyncio.run(_runner())
    except Exception:
        arms = {}

    # Fuse into a candidate pool, then re-rank down to top_k. Pool kept modest so
    # the re-rank prompt stays small (latency).
    pool = _fuse_and_rank(arms, top_k=max(top_k * 2, 16), prefer_calc=prefer_calc,
                          critical_paths=critical_paths)
    if rerank and len(pool) > top_k:
        return _llm_rerank(query, pool, top_k)
    return pool[:top_k]
