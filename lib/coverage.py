"""Coverage matrix + self-healing queue helpers.

Per bible §6 strategy 6: track per-entity gaps, surface them in the UI, and
feed a queue of "failed questions" that drives auto-fill.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from psycopg.rows import dict_row

from lib.db import get_conn


@dataclass
class RepoCoverageRow:
    repo_id: UUID
    display_name: str
    total_entities: int
    pass1_count: int
    pass1_verified_count: int
    statement_annotated_count: int
    git_history_count: int
    multi_example_count: int
    module_doc_covered_count: int


def repo_coverage() -> list[RepoCoverageRow]:
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM repo_coverage_summary ORDER BY display_name")
        rows = cur.fetchall()
    return [RepoCoverageRow(**r) for r in rows]


def entity_gaps(
    repo_id: UUID | None = None,
    *,
    limit: int = 100,
    only_kinds: tuple[str, ...] | None = None,
) -> list[dict]:
    """Return a list of entities with their per-perspective coverage flags."""
    sql = """
        SELECT ec.*
          FROM entity_coverage ec
         WHERE 1=1
    """
    params: list = []
    if repo_id is not None:
        sql += " AND ec.repo_id = %s"
        params.append(repo_id)
    if only_kinds:
        sql += " AND ec.kind = ANY(%s)"
        params.append(list(only_kinds))
    sql += """
         ORDER BY
           CASE WHEN NOT has_pass1 THEN 0
                WHEN NOT pass1_verified THEN 1
                WHEN NOT file_has_module_doc THEN 2
                WHEN NOT has_statement_annotations THEN 3
                ELSE 4
           END,
           ec.qualified_name
         LIMIT %s
    """
    params.append(limit)
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, tuple(params))
        return list(cur.fetchall())


def log_failed_question(
    *,
    question: str,
    product_id: UUID | None,
    session_id: UUID | None = None,
    user_id: str | None = None,
    refusal_reason: str | None = None,
    retrieved_count: int = 0,
    used_count: int = 0,
    suspected_entities: list[UUID] | None = None,
) -> UUID:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO failed_questions
              (session_id, user_id, product_id, question,
               refusal_reason, retrieved_count, used_count, suspected_entities)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING failed_id
            """,
            (session_id, user_id, product_id, question,
             refusal_reason, retrieved_count, used_count,
             list(suspected_entities) if suspected_entities else None),
        )
        fid = cur.fetchone()[0]
        conn.commit()
        return fid


def list_open_failed_questions(limit: int = 50) -> list[dict]:
    sql = """
        SELECT fq.failed_id, fq.question, fq.refusal_reason,
               fq.retrieved_count, fq.used_count,
               fq.suspected_entities, fq.created_at,
               p.name AS product_name
          FROM failed_questions fq
          LEFT JOIN products p ON p.product_id = fq.product_id
         WHERE fq.addressed_at IS NULL
         ORDER BY fq.created_at DESC
         LIMIT %s
    """
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, (limit,))
        return list(cur.fetchall())


def mark_failed_addressed(failed_id: UUID) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE failed_questions SET addressed_at = now() WHERE failed_id = %s",
            (failed_id,),
        )
        conn.commit()
