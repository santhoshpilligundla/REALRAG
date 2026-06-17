"""Inspect recent chat activity to diagnose answer quality."""
from __future__ import annotations
import sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.db import get_conn

with get_conn() as conn:
    print("=== recent messages (last 12) ===")
    try:
        rows = conn.execute(
            "SELECT role, left(content, 400), created_at FROM messages ORDER BY created_at DESC LIMIT 12"
        ).fetchall()
        for r in rows:
            print(f"\n[{str(r[2])[:19]} {r[0]}]\n{r[1]}")
    except Exception as e:
        print("  messages query failed:", e)

    print("\n\n=== recent failed_questions (last 10) ===")
    try:
        rows = conn.execute(
            "SELECT created_at, refusal_reason, retrieved_count, used_count, left(question,160) "
            "FROM failed_questions ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
        for r in rows:
            print(f"  {str(r[0])[:19]}  reason={r[1]}  retrieved={r[2]} used={r[3]}  q={r[4]!r}")
    except Exception as e:
        print("  failed_questions query failed:", e)
