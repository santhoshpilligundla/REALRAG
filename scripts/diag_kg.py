from __future__ import annotations
import sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.db import get_conn
from lib.kg import writes_to_table, reads_from_table

with get_conn() as c:
    print("=== facts by predicate (top 20) ===")
    for r in c.execute("SELECT predicate, count(*) FROM facts GROUP BY predicate ORDER BY count(*) DESC LIMIT 20").fetchall():
        print(f"  {r[0]:<24} {r[1]:,}")
    n = c.execute("SELECT count(*) FROM facts WHERE lower(object) LIKE '%seasonalforecast%'").fetchone()[0]
    print(f"\nfacts with object~seasonalforecast: {n}")
    for r in c.execute("SELECT subject, predicate, object FROM facts WHERE lower(object) LIKE '%seasonalforecast%' LIMIT 8").fetchall():
        print("   ", r)

print("\n=== writes_to_table('seasonalforecast') ===")
try:
    w = writes_to_table("seasonalforecast", product_id=None)
    print("  rows:", len(w))
    for x in w[:5]:
        print("   ", x.get("qualified_name"), x.get("repo_name"))
except Exception as e:
    print("  ERROR:", type(e).__name__, e)
