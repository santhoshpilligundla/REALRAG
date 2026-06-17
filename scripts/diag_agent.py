import sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.db import get_conn
from lib.agent import run_agent

with get_conn() as c:
    pid = c.execute("SELECT product_id FROM products WHERE name='RMS'").fetchone()[0]

q = sys.argv[1] if len(sys.argv) > 1 else "How is Forecasted Leases calculated in the Forecast & Optimization widget? Give the formula."
res = run_agent(q, product_id=pid, on_step=lambda s: print("  step:", s))
print(f"\n=== steps={res['steps']} cites={len(res['citations'])} ===")
print("trace:", res["trace"])
print("\nANSWER:\n", res["answer"])
