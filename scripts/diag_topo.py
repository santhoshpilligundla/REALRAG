import sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.db import get_conn
from lib.chat import prepare_answer, stream_answer, answer_looks_unsure, answer_has_fabrication

with get_conn() as c:
    pid = c.execute("SELECT product_id FROM products WHERE name='RMS'").fetchone()[0]

q = "There is an ETL called TOPriceOptimizer. what does it do"
prep = prepare_answer(q, product_id=pid)
print("=== retrieved sources (fast path) ===")
for c in prep.get("citations", [])[:8]:
    print("   ", c.get("repo"), c.get("qname"), c.get("file"))
ans = prep.get("answer") if prep["mode"] == "final" else "".join(stream_answer(prep))
print("\nunsure?", answer_looks_unsure(ans), " fabrication?", answer_has_fabrication(ans, prep.get("context_text","")))
print("ANSWER:\n", ans[:500])

print("\n=== is the authoritative source even indexed? ===")
with get_conn() as conn:
    for label, pat in [("entities ~ ToPriceOptimizer/EtlToPO", "%topriceoptimizer%"),
                       ("entities ~ etl2posql", "%etl2posql%"),
                       ("entities ~ EtlToPOExecutor", "%etltopo%")]:
        n = conn.execute("SELECT count(*) FROM entities WHERE lower(qualified_name) LIKE %s OR lower(name) LIKE %s", (pat, pat)).fetchone()[0]
        print(f"   {label}: {n}")
    n = conn.execute("SELECT count(*) FROM code_chunks WHERE lower(content) LIKE '%topriceoptimizer%'").fetchone()[0]
    print(f"   code_chunks mentioning 'topriceoptimizer': {n}")
    n = conn.execute("SELECT count(*) FROM generated_docs WHERE lower(behavioral) LIKE '%topriceoptimizer%' OR lower(business) LIKE '%topriceoptimizer%' OR lower(structural) LIKE '%topriceoptimizer%'").fetchone()[0]
    print(f"   generated_docs mentioning 'topriceoptimizer': {n}")
    n = conn.execute("SELECT count(*) FROM repo_files WHERE lower(path) LIKE '%pologschema%' OR lower(path) LIKE '%etl2posql%'").fetchone()[0]
    print(f"   repo_files POLogSchema/etl2posql indexed: {n}")
