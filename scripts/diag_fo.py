import sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.db import get_conn
from lib.chat import prepare_answer, stream_answer, _is_calc_question

with get_conn() as c:
    pid = c.execute("SELECT product_id FROM products WHERE name='RMS'").fetchone()[0]
q = "How is Forecasted Leases calculated in the Forecast & Optimization widget"
print("calc_question:", _is_calc_question(q))
prep = prepare_answer(q, product_id=pid)
print("mode:", prep["mode"])
for c in prep.get("citations", [])[:8]:
    print("   ", c.get("repo", ""), c.get("qname"), c.get("file"))
ans = prep.get("answer") if prep["mode"] == "final" else "".join(stream_answer(prep))
print("\nANSWER:\n", ans[:800])
