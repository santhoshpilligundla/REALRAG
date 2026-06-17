import sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.db import get_conn

targets = ["etl2posql.xml", "yscore_queries.xml", "airm_queries.xml", "chartsql.xml",
           "revenueforecaster_queries.xml", "fcstqueries.xml", "forecastqueries.xml",
           "seasonalityqueries.xml", "ysfcstqueries.xml", "queries.xml"]

with get_conn() as c, c.cursor() as cur:
    print(f"{'file':<42} {'files':>5} {'entities':>9} {'chunks':>7}")
    print("-" * 70)
    for t in targets:
        cur.execute("SELECT file_id FROM repo_files WHERE path ILIKE %s", (f"%{t}%",))
        fids = [r[0] for r in cur.fetchall()]
        if not fids:
            print(f"{t:<42} {'0':>5} {'(not walked)':>9}")
            continue
        cur.execute("SELECT count(*) FROM entities WHERE file_id = ANY(%s)", (fids,))
        ne = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM code_chunks WHERE file_id = ANY(%s)", (fids,))
        nc = cur.fetchone()[0]
        print(f"{t:<42} {len(fids):>5} {ne:>9} {nc:>7}")
