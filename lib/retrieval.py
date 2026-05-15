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

from lib.db import get_conn
from lib.embedder import embed_texts
from lib.faiss_store import search as faiss_search
from lib.kg import find_entity_by_name


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


def symbol_arm(structural_targets: list[str], product_id: UUID | None = None) -> list[Hit]:
    out: list[Hit] = []
    for target in structural_targets:
        rows = find_entity_by_name(target, product_id=product_id, limit=3)
        for row in rows:
            out.append(Hit(
                chunk_id=None,
                entity_id=row.get("entity_id"),
                repo_id=row["repo_id"],
                repo_name=row.get("repo_name") or "",
                file_path=row.get("file_path"),
                qualified_name=row.get("qualified_name"),
                kind=row.get("kind"),
                content=f"{row.get('kind')} {row.get('qualified_name')} (in {row.get('file_path')})",
                citations=[{
                    "file": row.get("file_path") or "",
                    "qname": row.get("qualified_name") or "",
                    "repo": row.get("repo_name") or "",
                }],
                score=1.0,
                source="symbol",
                pass_level="raw_chunk",
            ))
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _dedup_and_rank(hits: list[Hit], top_k: int = 8, score_floor: float = 0.0) -> list[Hit]:
    """Score-floor + dedup-by-entity. Cross-encoder re-rank deferred."""
    seen: set = set()
    out: list[Hit] = []
    for h in sorted(hits, key=lambda x: -x.score):
        if h.score < score_floor:
            continue
        key = (h.entity_id, h.pass_level) if h.entity_id else (h.qualified_name, h.source)
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
        if len(out) >= top_k:
            break
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
) -> list[Hit]:
    """Run all arms in parallel (sync via asyncio.gather + to_thread), merge, dedup, return top-k."""
    async def _runner() -> list[Hit]:
        # Embed query (and optionally HyDE).
        embed_text = rewrite
        if hyde:
            embed_text = rewrite + "\n\n" + hyde

        loop = asyncio.get_event_loop()

        embed_task = loop.run_in_executor(None, embed_texts, [embed_text])
        fts_task = loop.run_in_executor(None, fts_arm, rewrite, repo_ids, 10)
        sym_task = loop.run_in_executor(None, symbol_arm, structural_targets, product_id)

        (matrix, _model), fts_hits, sym_hits = await asyncio.gather(embed_task, fts_task, sym_task)
        if matrix.size == 0:
            return list(fts_hits) + list(sym_hits)

        qvec = matrix[0]

        code_task = loop.run_in_executor(None, vector_arm_code, qvec, repo_ids, 10)
        gen_task  = loop.run_in_executor(None, vector_arm_generated, qvec, repo_ids, 10)
        code_hits, gen_hits = await asyncio.gather(code_task, gen_task)

        return list(code_hits) + list(gen_hits) + list(fts_hits) + list(sym_hits)

    try:
        all_hits = asyncio.run(_runner())
    except Exception:
        all_hits = []

    return _dedup_and_rank(all_hits, top_k=top_k, score_floor=0.40)
