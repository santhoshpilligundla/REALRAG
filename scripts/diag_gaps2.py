import sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.db import get_conn

with get_conn() as c, c.cursor() as cur:
    print("=== po .sql files (all 0 entities) — what are they? ===")
    cur.execute("""
        SELECT rf.path FROM repo_files rf JOIN repos r ON r.repo_id=rf.repo_id
        WHERE r.display_name='po' AND rf.language='sql' ORDER BY rf.path LIMIT 25
    """)
    for (p,) in cur.fetchall():
        print("   ", p)

    print("\n=== rm-web TypeScript 0-entity files — characterize ===")
    cur.execute("""
        SELECT rf.path FROM repo_files rf JOIN repos r ON r.repo_id=rf.repo_id
        WHERE r.display_name='rm-web' AND rf.language='typescript'
          AND NOT EXISTS (SELECT 1 FROM entities e WHERE e.file_id=rf.file_id)
        ORDER BY rf.path
    """)
    ts = [p for (p,) in cur.fetchall()]
    import collections
    cats = collections.Counter()
    for p in ts:
        pl = p.lower()
        if pl.endswith(".spec.ts"): cats["spec/test"] += 1
        elif pl.endswith(".module.ts"): cats["module"] += 1
        elif pl.endswith(".d.ts"): cats["type-decl"] += 1
        elif pl.endswith("index.ts") or pl.endswith("/index.ts"): cats["index/barrel"] += 1
        elif "routing" in pl: cats["routing"] += 1
        elif pl.endswith(".model.ts") or "/models/" in pl: cats["model/interface"] += 1
        elif pl.endswith(".const.ts") or "constant" in pl: cats["constants"] += 1
        else: cats["OTHER (component/service?)"] += 1
    for k, v in cats.most_common():
        print(f"   {k}: {v}")
    print("   --- sample OTHER ---")
    shown = 0
    for p in ts:
        pl = p.lower()
        if not (pl.endswith((".spec.ts",".module.ts",".d.ts","index.ts",".model.ts",".const.ts")) or "routing" in pl or "/models/" in pl or "constant" in pl):
            print("     ", p); shown += 1
            if shown >= 12: break
