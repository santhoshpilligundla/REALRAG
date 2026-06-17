"""Per-arm retrieval diagnostics: counts, score ranges, survivors past the 0.40 floor."""
from __future__ import annotations

import sys
import statistics as stats
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import get_conn
from lib.intent import classify
from lib.embedder import embed_texts
from lib import retrieval as R

FLOOR = 0.40

def _pid():
    with get_conn() as c:
        return c.execute("SELECT product_id FROM products WHERE name='RMS'").fetchone()[0]

def _repo_ids(pid):
    with get_conn() as c:
        return [r[0] for r in c.execute("SELECT repo_id FROM repos WHERE product_id=%s", (pid,)).fetchall()]

def summ(name, hits):
    scores = [h.score for h in hits]
    if not scores:
        print(f"  {name:<16} n=0")
        return
    surv = sum(1 for s in scores if s >= FLOOR)
    print(f"  {name:<16} n={len(scores):<3} min={min(scores):.3f} med={stats.median(scores):.3f} "
          f"max={max(scores):.3f}  >= {FLOOR}: {surv}")

QS = [
    "What is Rates Generation Process",
    "How is recommendedRent calculated in the forecasting module?",
    "What writes to the seasonalforecast table?",
]

pid = _pid(); repo_ids = _repo_ids(pid)
for q in QS:
    print("=" * 80)
    print("Q:", q)
    it = classify(q)
    print(f"  intent={it.intent} tier={it.suggested_tier} targets={it.structural_targets}")
    print(f"  rewrite={it.rewrite!r}")
    print(f"  hyde={(it.hyde or '')[:120]!r}")
    embed_text = it.rewrite + (("\n\n" + it.hyde) if it.hyde else "")
    qvec = embed_texts([embed_text])[0][0]
    sym_targets = R._symbolic_targets(it.structural_targets)
    print(f"  symbol_targets(filtered)={sym_targets}")
    code = [h for h in R.vector_arm_code(qvec, repo_ids, 10) if h.score >= R._VECTOR_MIN_COS]
    gen  = [h for h in R.vector_arm_generated(qvec, repo_ids, 10) if h.score >= R._VECTOR_MIN_COS]
    fts  = R.fts_arm(it.rewrite, repo_ids, 10)
    sym  = R.symbol_arm(sym_targets, pid)
    print("  --- per-arm scores ---")
    summ("vector(code)", code)
    summ("vector(gen)", gen)
    summ("fts", fts)
    summ("symbol", sym)
    final = R._fuse_and_rank(
        {"vector_code": code, "vector_gen": gen, "fts": fts, "symbol": sym}, top_k=8)
    print(f"  --- final top-{len(final)} (RRF + doc boost) ---")
    for h in final:
        print(f"     {h.score:.3f} {h.source:<8} {h.pass_level:<10} {h.repo_name}:{h.qualified_name}")
    print(f"  final sources: {[h.source for h in final]}")
