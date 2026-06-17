import sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.db import get_conn
from lib.faiss_store import load_index
load_index.cache_clear()  # ensure fresh index in this process
from lib.chat import prepare_answer, stream_answer, context_covers_subjects

with get_conn() as c:
    pid = c.execute("SELECT product_id FROM products WHERE name='RMS'").fetchone()[0]
    # confirm the new content is indexed
    n_sql = c.execute("SELECT count(*) FROM entities WHERE kind IN ('sql_function','sql_procedure','sql_script','sql_view')").fetchone()[0]
    n_xmlsvc = c.execute("SELECT count(*) FROM entities WHERE kind='xml_service'").fetchone()[0]
    n_funcdoc = c.execute("""SELECT count(*) FROM generated_docs d JOIN entities e ON e.entity_id=d.entity_id
                             WHERE e.kind IN ('sql_function','sql_procedure')""").fetchone()[0]
print(f"indexed: sql_* entities={n_sql}  xml_service={n_xmlsvc}  sql-func docs={n_funcdoc}\n")

for q in ["What types of data does the TOPriceOptimizer ETL pull?",
          "How is market effective max (MktEffMax) calculated?"]:
    prep = prepare_answer(q, product_id=pid)
    ans = prep.get("answer") if prep["mode"] == "final" else "".join(stream_answer(prep))
    print("="*80)
    print("Q:", q)
    print("  sources:", [c.get("qname") or c.get("file","")[:40] for c in prep.get("citations",[])[:6]])
    print("  ", (ans or "")[:500])
