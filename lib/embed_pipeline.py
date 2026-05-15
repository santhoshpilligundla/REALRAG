"""Bulk embed: code chunks + generated docs for a repo. Writes per-repo FAISS files."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from uuid import UUID

import numpy as np

from lib.db import get_conn
from lib.embedder import embed_texts
from lib.faiss_store import write_index
from lib.runs_repo import finish_run, start_run


@dataclass
class EmbedResult:
    success: bool
    code_chunks_embedded: int
    generated_docs_embedded: int
    error: str | None = None


def _doc_text(structural: str, behavioral: str, business: str | None,
              edge_cases: str | None, cross_references: str | None) -> str:
    parts = [structural, behavioral]
    if business:
        parts.append(f"BUSINESS: {business}")
    if edge_cases:
        parts.append(f"EDGE CASES: {edge_cases}")
    if cross_references:
        parts.append(f"CROSS REFS: {cross_references}")
    return "\n\n".join(parts)


def count_pending_embeds(repo_id: UUID) -> dict[str, int]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM code_chunks WHERE repo_id = %s AND embedding_model IS NULL",
            (repo_id,),
        )
        code = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM generated_docs WHERE repo_id = %s AND embedding_model IS NULL",
            (repo_id,),
        )
        docs = cur.fetchone()[0]
        return {"code_chunks": code, "generated_docs": docs}


def _fetch_all_code_chunks(repo_id: UUID) -> tuple[list[UUID], list[str]]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT chunk_id, content FROM code_chunks WHERE repo_id = %s ORDER BY chunk_id",
            (repo_id,),
        )
        rows = cur.fetchall()
    if not rows:
        return [], []
    ids = [r[0] for r in rows]
    texts = [r[1] for r in rows]
    return ids, texts


def _fetch_all_generated_docs(repo_id: UUID) -> tuple[list[UUID], list[str]]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT doc_id, structural, behavioral, business, edge_cases, cross_references
              FROM generated_docs
             WHERE repo_id = %s
             ORDER BY doc_id
            """,
            (repo_id,),
        )
        rows = cur.fetchall()
    if not rows:
        return [], []
    ids = [r[0] for r in rows]
    texts = [_doc_text(r[1], r[2], r[3], r[4], r[5]) for r in rows]
    return ids, texts


def _mark_embedded(table: str, ids: list[UUID], model: str) -> None:
    if not ids:
        return
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {table}
               SET embedding_model = %s,
                   faiss_ord = subq.ord
              FROM (SELECT id_value, ord
                      FROM unnest(%s::uuid[]) WITH ORDINALITY AS t(id_value, ord)) AS subq
             WHERE {table}.{'chunk_id' if table == 'code_chunks' else 'doc_id'} = subq.id_value
            """,
            (model, ids),
        )
        conn.commit()


def embed_repo(
    repo_id: UUID,
    on_progress: Callable[[str], None] | None = None,
) -> EmbedResult:
    """Embed all code chunks AND all generated docs for a repo. Writes per-repo FAISS."""
    cb = on_progress or (lambda _msg: None)

    pending = count_pending_embeds(repo_id)
    if pending["code_chunks"] == 0 and pending["generated_docs"] == 0:
        cb("nothing to embed (already up to date)")
        return EmbedResult(True, 0, 0)

    cb(f"embedding — code_chunks_pending={pending['code_chunks']}, "
       f"generated_docs_pending={pending['generated_docs']}")

    run_id = start_run(repo_id, "embed", notes=f"pending={pending}")

    code_done = 0
    docs_done = 0

    try:
        ids, texts = _fetch_all_code_chunks(repo_id)
        if ids:
            cb(f"embedding {len(ids)} code chunks…")
            matrix, model = embed_texts(texts, batch_size=128)
            cb(f"writing FAISS index (code, {len(ids)} vectors)…")
            write_index(repo_id, "code", matrix, ids, model=model)
            _mark_embedded("code_chunks", ids, model)
            code_done = len(ids)

        ids, texts = _fetch_all_generated_docs(repo_id)
        if ids:
            cb(f"embedding {len(ids)} generated docs…")
            matrix, model = embed_texts(texts, batch_size=128)
            cb(f"writing FAISS index (generated, {len(ids)} vectors)…")
            write_index(repo_id, "generated", matrix, ids, model=model)
            _mark_embedded("generated_docs", ids, model)
            docs_done = len(ids)
    except Exception as e:
        finish_run(run_id, "error", error_message=f"{type(e).__name__}: {e}",
                   counts={"code_chunks": code_done, "generated_docs": docs_done})
        return EmbedResult(False, code_done, docs_done, str(e))

    counts = {"code_chunks": code_done, "generated_docs": docs_done}
    finish_run(run_id, "success", counts=counts)
    cb(f"done — code={code_done} docs={docs_done}")
    return EmbedResult(True, code_done, docs_done)
