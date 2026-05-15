"""Knowledge-graph queries on facts + cross_repo_edges + dependencies.

Per bible §11: "Knowledge-graph triples query — some questions ('what writes
to table X?') are answered by SQL on triples — provably 100% accurate, no LLM."

These functions are called by the chat tier router for structural questions
that have deterministic answers.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg.rows import dict_row

from lib.db import get_conn


def writes_to_table(table_name: str, product_id: UUID | None = None) -> list[dict]:
    """Find every entity that writes to a given table (subject of a writes_table fact)."""
    sql = """
        SELECT DISTINCT
               e.entity_id, e.qualified_name, e.kind,
               r.display_name AS repo_name,
               r.repo_id      AS repo_id,
               f.path         AS file_path,
               fa.confidence
          FROM facts fa
          JOIN entities e ON e.entity_id = fa.entity_id
          JOIN repos r    ON r.repo_id = e.repo_id
          LEFT JOIN repo_files f ON f.file_id = e.file_id
         WHERE fa.predicate = 'writes_table'
           AND lower(fa.object) = lower(%s)
    """
    params: list = [table_name]
    if product_id is not None:
        sql += " AND r.product_id = %s"
        params.append(product_id)
    sql += " ORDER BY r.display_name, e.qualified_name"
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, tuple(params))
        return list(cur.fetchall())


def reads_from_table(table_name: str, product_id: UUID | None = None) -> list[dict]:
    sql = """
        SELECT DISTINCT
               e.entity_id, e.qualified_name, e.kind,
               r.display_name AS repo_name,
               r.repo_id      AS repo_id,
               f.path         AS file_path,
               fa.confidence
          FROM facts fa
          JOIN entities e ON e.entity_id = fa.entity_id
          JOIN repos r    ON r.repo_id = e.repo_id
          LEFT JOIN repo_files f ON f.file_id = e.file_id
         WHERE fa.predicate = 'reads_table'
           AND lower(fa.object) = lower(%s)
    """
    params: list = [table_name]
    if product_id is not None:
        sql += " AND r.product_id = %s"
        params.append(product_id)
    sql += " ORDER BY r.display_name, e.qualified_name"
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, tuple(params))
        return list(cur.fetchall())


def trace_chain_from(start_entity_id: UUID, max_depth: int = 6) -> list[dict]:
    """Recursive walk of cross_repo_edges starting at the given entity.

    Returns the chain as ordered nodes (depth ascending) — one of the bible's
    headline retrieval patterns (§7.4 cross-repo trace).
    """
    sql = """
        WITH RECURSIVE chain (entity_id, depth, path, edge_kind) AS (
            SELECT %s::uuid, 0, ARRAY[%s::uuid], CAST(NULL AS TEXT)
            UNION ALL
            SELECT cre.to_entity_id, c.depth + 1, c.path || cre.to_entity_id, cre.kind
              FROM chain c
              JOIN cross_repo_edges cre ON cre.from_entity_id = c.entity_id
             WHERE c.depth < %s
               AND NOT (cre.to_entity_id = ANY(c.path))
        )
        SELECT chain.depth, chain.edge_kind,
               e.entity_id, e.qualified_name, e.kind,
               r.display_name AS repo_name,
               r.repo_id      AS repo_id,
               f.path         AS file_path
          FROM chain
          JOIN entities e ON e.entity_id = chain.entity_id
          JOIN repos r    ON r.repo_id = e.repo_id
          LEFT JOIN repo_files f ON f.file_id = e.file_id
         ORDER BY chain.depth
    """
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, (start_entity_id, start_entity_id, max_depth))
        return list(cur.fetchall())


def find_entity_by_name(name: str, product_id: UUID | None = None, limit: int = 5) -> list[dict]:
    """Symbol exact-match resolver — bible §7.2 'symbol exact-match' arm."""
    sql = """
        SELECT e.entity_id, e.qualified_name, e.kind, e.name,
               r.display_name AS repo_name,
               r.repo_id      AS repo_id,
               f.path         AS file_path
          FROM entities e
          JOIN repos r    ON r.repo_id = e.repo_id
          LEFT JOIN repo_files f ON f.file_id = e.file_id
         WHERE (lower(e.name) = lower(%s) OR lower(e.qualified_name) = lower(%s))
    """
    params: list = [name, name]
    if product_id is not None:
        sql += " AND r.product_id = %s"
        params.append(product_id)
    sql += " ORDER BY length(e.qualified_name) LIMIT %s"
    params.append(limit)
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, tuple(params))
        return list(cur.fetchall())


def edges_outgoing(entity_id: UUID) -> list[dict]:
    sql = """
        SELECT cre.kind, cre.confidence, cre.discovered_via,
               te.qualified_name AS to_qname, te.kind AS to_kind,
               tr.display_name AS to_repo
          FROM cross_repo_edges cre
          JOIN entities te ON te.entity_id = cre.to_entity_id
          JOIN repos tr    ON tr.repo_id = te.repo_id
         WHERE cre.from_entity_id = %s
         ORDER BY cre.confidence DESC, cre.kind
    """
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, (entity_id,))
        return list(cur.fetchall())


def edges_incoming(entity_id: UUID) -> list[dict]:
    sql = """
        SELECT cre.kind, cre.confidence,
               fe.qualified_name AS from_qname, fe.kind AS from_kind,
               fr.display_name AS from_repo
          FROM cross_repo_edges cre
          JOIN entities fe ON fe.entity_id = cre.from_entity_id
          JOIN repos fr    ON fr.repo_id = fe.repo_id
         WHERE cre.to_entity_id = %s
         ORDER BY cre.confidence DESC, cre.kind
    """
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, (entity_id,))
        return list(cur.fetchall())
