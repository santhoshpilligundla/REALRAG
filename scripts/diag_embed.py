"""Diagnose why the embed step wrote 0 vectors."""
from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import get_conn

print("=== recent embed runs ===")
with get_conn() as conn:
    rows = conn.execute(
        "SELECT started_at, status, error_message, counts FROM pipeline_runs "
        "WHERE stage = 'embed' ORDER BY started_at DESC LIMIT 6"
    ).fetchall()
    for r in rows:
        print(f"  {str(r[0])[:19]}  status={r[1]}  err={r[2]}  counts={r[3]}")

print("\n=== direct single-call embedding test ===")
try:
    from lib.embedder import embed_texts
    mat, model = embed_texts(["hello world", "def foo(): return 1"], batch_size=128)
    print(f"  OK  shape={mat.shape}  model={model}")
except Exception as e:
    import traceback
    print(f"  FAILED: {type(e).__name__}: {e}")
    traceback.print_exc()
