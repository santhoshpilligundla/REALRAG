from __future__ import annotations
import sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.db import get_conn

CORE = ["RenewalRatesBuilder%", "RenewalRentServiceImpl%", "BulkGenerateRenewalsFilterAction%", "BatchRenewalHelper"]
with get_conn() as c:
    print("###### CORE ENTITY DOCS ######")
    for pat in CORE:
        r = c.execute("""
            SELECT e.qualified_name, d.behavioral, d.business
              FROM generated_docs d JOIN entities e ON e.entity_id=d.entity_id
             WHERE d.pass_level='entity' AND e.qualified_name ILIKE %s
             ORDER BY (d.depth_tier='L4') DESC LIMIT 1
        """, (pat,)).fetchone()
        if r:
            print(f"\n=== {r[0]} ===")
            print("BEHAVIORAL:", (r[1] or "")[:700])
            print("BUSINESS:", (r[2] or "")[:700])

    print("\n\n###### RENEWAL NARRATIVES ######")
    for r in c.execute("""
        SELECT narrative_subject, behavioral FROM generated_docs
         WHERE pass_level='narrative' AND (narrative_subject ILIKE '%renewal%' OR behavioral ILIKE '%renewal rate%')
         LIMIT 4
    """).fetchall():
        print(f"\n=== {r[0]} ===")
        print((r[1] or "")[:800])
