import sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.db import get_conn

with get_conn() as c, c.cursor() as cur:
    try:
        cur.execute("SELECT count(*) FROM query_cache")
        n = cur.fetchone()[0]
        cur.execute("DELETE FROM query_cache")
        c.commit()
        print(f"cleared {n} cached answers (stale pre-fix answers removed)")
    except Exception as e:
        print("no query_cache table yet / nothing to clear:", e)
