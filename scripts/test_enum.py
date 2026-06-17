import sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.faiss_store import load_index
load_index.cache_clear()
from lib.db import get_conn
from lib.chat import prepare_answer, stream_answer, _is_enumeration

q = "TOPriceOptimizer ETL gets what all information from other Property Management Systems"
print("enumeration?", _is_enumeration(q))
with get_conn() as c:
    pid = c.execute("SELECT product_id FROM products WHERE name='RMS'").fetchone()[0]
prep = prepare_answer(q, product_id=pid)
ans = "".join(stream_answer(prep)) if prep["mode"] != "final" else prep["answer"]
print("\nANSWER:\n", ans[:1600])
