"""Diagnose why a specific term/question retrieves poorly. Usage: diag_term.py "question" "searchterm" """
from __future__ import annotations
import sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.db import get_conn
from lib.intent import classify
from lib.embedder import embed_texts
from lib import retrieval as R
from lib.chat import chat

QUESTION = sys.argv[1] if len(sys.argv) > 1 else "What are In Place Units?"
TERM = sys.argv[2] if len(sys.argv) > 2 else "in place"

def pid():
    with get_conn() as c:
        return c.execute("SELECT product_id FROM products WHERE name='RMS'").fetchone()[0]
def repo_ids(p):
    with get_conn() as c:
        return [r[0] for r in c.execute("SELECT repo_id FROM repos WHERE product_id=%s",(p,)).fetchall()]

P = pid(); RIDS = repo_ids(P)

print("="*80); print("QUESTION:", QUESTION)
it = classify(QUESTION)
print(f"  intent={it.intent} tier={it.suggested_tier} targets={it.structural_targets}")
print(f"  rewrite={it.rewrite!r}")

et = it.rewrite + (("\n\n"+it.hyde) if it.hyde else "")
qvec = embed_texts([et])[0][0]
sym = R.symbol_arm(R._symbolic_targets(it.structural_targets), P)
code = [h for h in R.vector_arm_code(qvec, RIDS, 10) if h.score >= R._VECTOR_MIN_COS]
gen  = [h for h in R.vector_arm_generated(qvec, RIDS, 10) if h.score >= R._VECTOR_MIN_COS]
fts  = R.fts_arm(it.rewrite, RIDS, 10)
final = R._fuse_and_rank({"vc":code,"vg":gen,"fts":fts,"sym":sym}, top_k=8)
print(f"  arms: code={len(code)} gen={len(gen)} fts={len(fts)} sym={len(sym)}")
print("  top hits:")
for h in final:
    print(f"     {h.score:.3f} {h.source:<7} {h.pass_level:<9} {h.repo_name}:{h.qualified_name}")

print("\n--- glossary entries matching term ---")
with get_conn() as c:
    for r in c.execute("SELECT term, definition FROM domain_glossary WHERE term ILIKE %s OR definition ILIKE %s LIMIT 10",(f"%{TERM}%",f"%{TERM}%")).fetchall():
        print(f"   {r[0]}: {(r[1] or '')[:160]}")
    print("\n--- generated_docs mentioning term (count) ---")
    n = c.execute("SELECT count(*) FROM generated_docs WHERE business ILIKE %s OR behavioral ILIKE %s OR structural ILIKE %s",(f"%{TERM}%",f"%{TERM}%",f"%{TERM}%")).fetchone()[0]
    print(f"   docs mentioning '{TERM}': {n}")
    for r in c.execute("SELECT e.qualified_name, left(d.business,200) FROM generated_docs d JOIN entities e ON e.entity_id=d.entity_id WHERE d.business ILIKE %s LIMIT 5",(f"%{TERM}%",)).fetchall():
        print(f"     {r[0]}: {r[1]}")

print("\n--- CHAT answer ---")
a = chat(QUESTION, product_id=P)
print(f"  refusal={a.refusal} faithful={a.is_faithful} used={a.used_hits}/{a.retrieved_hits}")
print((a.answer or "")[:1500])
