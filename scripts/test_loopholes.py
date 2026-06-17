import sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.chat import (_question_subjects, context_covers_subjects, wants_exact,
                      prepare_answer, stream_answer)
from lib.db import get_conn

print("=== subject extraction ===")
for q in ["There is an ETL called TOPriceOptimizer. what does it do",
          "how does forecasting work", "What are In Place Units?"]:
    print(f"  {q!r} -> {_question_subjects(q)}")

print("\n=== subject-grounding (drift detection) ===")
print("  TOPO subject in forecasting ctx:",
      context_covers_subjects("what does TOPriceOptimizer do",
                              "This forecasting model computes recommended rent and seasonal adjustment."))
print("  TOPO subject in correct ctx:",
      context_covers_subjects("what does TOPriceOptimizer do",
                              "EtlToPOExecutor invokes the ToPriceOptimizer ETL load."))
print("  generic question (no subject):",
      context_covers_subjects("how does forecasting work", "anything"))

print("\n=== wants_exact ===")
print("  'give me the formula':", wants_exact("give me the formula for recommended rent"))
print("  'what is X':", wants_exact("what is the model threshold"))

print("\n=== end-to-end (must still answer correctly) ===")
with get_conn() as c:
    pid = c.execute("SELECT product_id FROM products WHERE name='RMS'").fetchone()[0]
for q in ["There is an ETL called TOPriceOptimizer. what does it do", "What are In Place Units?"]:
    prep = prepare_answer(q, product_id=pid)
    covered = context_covers_subjects(q, prep.get("context_text", ""))
    ans = prep.get("answer") if prep["mode"] == "final" else "".join(stream_answer(prep))
    print(f"\nQ: {q}\n  subject_covered={covered}\n  {ans[:220]}")
