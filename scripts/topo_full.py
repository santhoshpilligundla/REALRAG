import sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.db import get_conn

with get_conn() as c, c.cursor() as cur:
    cur.execute("""
        SELECT e.qualified_name FROM entities e JOIN repo_files f ON f.file_id=e.file_id
        WHERE f.path ILIKE '%etl2posql.xml%' AND e.kind='xml_service'
        ORDER BY e.start_line
    """)
    svcs = [r[0] for r in cur.fetchall()]
print(f"total etl2posql services indexed: {len(svcs)}")
for s in svcs:
    print("  ", s)
