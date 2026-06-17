import sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.db import get_conn
from lib.chat import prepare_answer, stream_answer, answer_has_fabrication, answer_looks_unsure
from lib.query_cache import cache_put, cache_get
from lib.business_docs import search_business_docs
from lib.embedder import embed_texts

with get_conn() as c:
    pid = c.execute("SELECT product_id FROM products WHERE name='RMS'").fetchone()[0]

print("=== fabrication detector ===")
print("  fake table flagged:", answer_has_fabrication("It reads RevenueForecastBedroomWeekly.", "FR.curleases fcstrecs"))
print("  clean business answer:", answer_has_fabrication("It uses the current leases and cancellations.", "anything"))

print("\n=== query cache ===")
cache_put(pid, "ZZ test question", "cached answer body", [{"file": "x"}], "T1")
print("  hit:", (cache_get(pid, "zz test  QUESTION") or {}).get("answer"))
with get_conn() as c:
    c.execute("DELETE FROM query_cache WHERE question='ZZ test question'"); c.commit()

print("\n=== business-docs arm ===")
qv = embed_texts(["how does RealRAG keep answers accurate"])[0][0]
for r in search_business_docs(qv, 3):
    print(f"   {r['source']} ({r['score']:.3f}): {r['content'][:80]}")

print("\n=== end-to-end answers (with biasing + business arm) ===")
for q in ["How is Forecasted Leases calculated in the Forecast & Optimization widget",
          "What are In Place Units?"]:
    prep = prepare_answer(q, product_id=pid)
    srcs = [c.get("repo") or c.get("file","")[:30] for c in prep.get("citations", [])[:4]]
    ans = prep.get("answer") if prep["mode"] == "final" else "".join(stream_answer(prep))
    print(f"\nQ: {q}\n  srcs={srcs}\n  {ans[:200]}")
