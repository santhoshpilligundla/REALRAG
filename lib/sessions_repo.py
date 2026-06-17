"""Persisted chat sessions + messages, so a conversation survives page reloads
and server restarts (durable continuous chat). Uses the sessions/messages tables
from schema 006.
"""
from __future__ import annotations

import json
from uuid import UUID

from psycopg.rows import dict_row

from lib.db import get_conn

_DEFAULT_USER = "local"


def create_session(product_id: UUID | None = None, user_id: str = _DEFAULT_USER) -> UUID:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sessions (user_id, product_id) VALUES (%s, %s) RETURNING session_id",
            (user_id, product_id),
        )
        sid = cur.fetchone()[0]
        conn.commit()
        return sid


def latest_session(user_id: str = _DEFAULT_USER) -> UUID | None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT session_id FROM sessions WHERE user_id = %s ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def save_message(session_id: UUID, role: str, content: str,
                 citations: list[dict] | None = None) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO messages (session_id, role, content, citations) "
            "VALUES (%s, %s, %s, %s::jsonb)",
            (session_id, role, content,
             json.dumps(citations, default=str) if citations else None),
        )
        conn.commit()


def load_messages(session_id: UUID) -> list[dict]:
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT role, content, citations FROM messages "
            "WHERE session_id = %s ORDER BY created_at",
            (session_id,),
        )
        return [
            {"role": r["role"], "content": r["content"], "citations": r["citations"] or []}
            for r in cur.fetchall()
        ]
