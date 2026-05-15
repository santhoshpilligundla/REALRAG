"""CRUD for pipeline_runs — what each pipeline stage did, when, with what counts."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row

from lib.db import get_conn


def start_run(repo_id: UUID | None, stage: str, notes: str | None = None) -> UUID:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pipeline_runs (repo_id, stage, status, notes)
            VALUES (%s, %s, 'running', %s)
            RETURNING run_id
            """,
            (repo_id, stage, notes),
        )
        run_id = cur.fetchone()[0]
        conn.commit()
        return run_id


def finish_run(
    run_id: UUID,
    status: str,
    counts: dict[str, Any] | None = None,
    error_message: str | None = None,
) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE pipeline_runs
               SET status = %s,
                   finished_at = now(),
                   elapsed_seconds = EXTRACT(EPOCH FROM (now() - started_at)),
                   counts = %s::jsonb,
                   error_message = %s
             WHERE run_id = %s
            """,
            (
                status,
                json.dumps(counts) if counts else None,
                error_message,
                run_id,
            ),
        )
        conn.commit()


def list_recent_runs(limit: int = 20) -> list[dict]:
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT r.run_id, r.repo_id, r.stage, r.status,
                   r.started_at, r.finished_at, r.elapsed_seconds,
                   r.counts, r.error_message, r.notes,
                   repos.display_name
              FROM pipeline_runs r
              LEFT JOIN repos USING (repo_id)
             ORDER BY r.started_at DESC
             LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def list_running() -> list[dict]:
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT r.run_id, r.repo_id, r.stage, r.started_at, r.notes,
                   repos.display_name
              FROM pipeline_runs r
              LEFT JOIN repos USING (repo_id)
             WHERE r.status = 'running'
             ORDER BY r.started_at DESC
            """,
        )
        return cur.fetchall()
