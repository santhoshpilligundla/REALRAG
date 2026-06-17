import sys
import time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from lib.agent_flows import load_flows_config, build_launch_plan  # noqa: E402
from lib.claude_runner import run_flow  # noqa: E402

cfg = load_flows_config()
cfg["run"]["max_turns"] = 6  # bound cost/time for this smoke test

q = "The forecasting ETL is not loading recommended rent for property 12345"
plan = build_launch_plan(q, product_id=None)
print(f"PLAN entry={plan['entry']} chain={plan['chain']} mode={plan['launch_mode']}")
print(f"  agent={plan['agent']} seq={plan.get('agent_sequence')}")
print(f"  cwd={plan['cwd']} add_dirs={plan['add_dirs']}")
print("=" * 70)

meta: dict = {}
start = time.time()
gen = run_flow(plan, q, meta)
got = 0
saw_delegation = False
for chunk in gen:
    sys.stdout.write(chunk)
    sys.stdout.flush()
    got += len(chunk)
    if "→ **" in chunk:
        saw_delegation = True
    if time.time() - start > 240:
        print("\n\n[TEST] 240s wall-clock cap — stopping (terminates the run).")
        gen.close()
        break
print(f"\n\n[META] {meta}")
print(f"[TEST] chars={got} saw_delegation={saw_delegation} elapsed={time.time()-start:.0f}s")
