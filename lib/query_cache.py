"""Exact query -> answer cache. Repeated questions return instantly — the best
defense against per-call network latency. Keyed by (product, normalized question).

Only used for standalone questions (no conversation context), so a cached answer
can't be wrong for a different follow-up thread.
"""
from __future__ import annotations

import hashlib
import json
import re
from uuid import UUID

from lib.db import get_conn

_READY = False


def _ensure_table() -> None:
    global _READY
    if _READY:
        return
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS query_cache (
                cache_key   TEXT PRIMARY KEY,
                product_id  UUID,
                question    TEXT NOT NULL,
                answer      TEXT NOT NULL,
                citations   JSONB,
                tier        TEXT,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        conn.commit()
    _READY = True


def _key(product_id: UUID | None, question: str) -> str:
    norm = re.sub(r"\s+", " ", (question or "").strip().lower())
    return hashlib.sha1(f"{product_id}|{norm}".encode("utf-8")).hexdigest()


def cache_get(product_id: UUID | None, question: str) -> dict | None:
    _ensure_table()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT answer, citations, tier FROM query_cache WHERE cache_key = %s",
            (_key(product_id, question),),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {"answer": row[0], "citations": row[1] or [], "tier": row[2]}


def cache_put(product_id: UUID | None, question: str, answer: str,
              citations: list[dict] | None, tier: str | None) -> None:
    if not answer:
        return
    _ensure_table()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO query_cache (cache_key, product_id, question, answer, citations, tier)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (cache_key) DO UPDATE SET
              answer = EXCLUDED.answer, citations = EXCLUDED.citations,
              tier = EXCLUDED.tier, created_at = now()
            """,
            (_key(product_id, question), product_id, question, answer,
             json.dumps(citations, default=str) if citations else None, tier),
        )
        conn.commit()
