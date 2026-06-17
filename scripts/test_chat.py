"""End-to-end chat smoke test — full answers + verifier flags, optional tier override."""
from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.chat import chat
from lib.db import get_conn


def _rms_product_id():
    with get_conn() as conn:
        row = conn.execute("SELECT product_id FROM products WHERE name = 'RMS'").fetchone()
        return row[0] if row else None


QUESTIONS = [
    "What is Rates Generation Process",
    "How is recommendedRent calculated in the forecasting module?",
    "What writes to the seasonalforecast table?",
]


def main() -> None:
    pid = _rms_product_id()
    print(f"RMS product_id = {pid}\n")
    for q in QUESTIONS:
        print("=" * 90)
        print(f"Q: {q}")
        ans = chat(q, product_id=pid)
        print(f"  intent={ans.intent} tier={ans.tier} refusal={ans.refusal} "
              f"is_faithful={getattr(ans,'is_faithful',None)} "
              f"used={getattr(ans,'used_hits','?')}/{getattr(ans,'retrieved_hits','?')}")
        print("  --- answer (full) ---")
        print((ans.answer or ""))
        print()


if __name__ == "__main__":
    main()
