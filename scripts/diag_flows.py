import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from lib.agent_flows import build_launch_plan  # noqa: E402

QS = [
    "The Forecasted Renewals widget shows the wrong value",
    "renewal rate looks wrong for property 12345",
    "Rent Roll report is missing some units",
    "TOPriceOptimizer ETL is not loading data",
    "how does the leasing screen submit a new lease",
]
for q in QS:
    try:
        p = build_launch_plan(q, product_id=None)
        print(f"\nQ: {q}")
        print(f"  entry={p['entry']}  chain={p['chain']}  mode={p['launch_mode']}")
        print(f"  agent={p['agent']}")
        print(f"  cwd={p['cwd']}")
        print(f"  add_dirs={p['add_dirs']}")
        print(f"  debug.tally={p['debug'].get('tally')} kw={p['debug'].get('keyword_hits')} "
              f"retr={p['debug'].get('retrieval_repos') or p['debug'].get('retrieval_error')}")
    except Exception as e:
        print(f"\nQ: {q}\n  ERROR {type(e).__name__}: {e}")
